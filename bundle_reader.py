#!/usr/bin/env python3
"""
bundle_reader.py — Canonical read-only reader for Hackathon Judge
run bundles (the format written by hackathon_judge.py).

Reads the current primary bundle layout:

    manifest/bundle.json        run_id, mode, status, command_log[] (embedded)
    inputs/<submission_id>.json write-once submission records
    verdicts/<submission_id>.json per-submission panel verdicts
    feedback/<submission_id>.json per-submission feedback cards
    sealed/shadow_score.json    write-once public-ranking scoring vault
    sealed/shadow_spec.json     hidden diagnostic criteria
    sealed/shadow_assessment.json  hidden diagnostic results
    winner/awards.json          declared award slate
    freshness_gate.json         model freshness check result

This module never writes to a bundle — it only opens files that already
exist. Two projections are exposed:

  * ``operator_view()``  — full fidelity, unredacted. For facilitators only.
  * ``audience_view()``  — safe for live display. Verdicts never carry
    ``total_score``/``dimension_scores`` until the manifest status reaches
    ``awarded`` or ``exported``, and submissions/verdicts are always
    presented in arrival order rather than score/rank order.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Manifest statuses at which scores/awards become safe to reveal.
REVEALED_STATUSES = frozenset({"awarded", "exported"})

_PRIVATE_PROJECT_CONTEXT_FIELDS = (
    "problem_statement",
    "intended_user",
    "demo_url",
    "builder_notes",
)
_NARRATIVE_SPOILER_RE = re.compile(
    r"\b(?:score(?:s|d)?|rank(?:s|ed|ing)?|leaderboard|winners?|winning|"
    r"finalists?|(?:\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth|sixth|"
    r"seventh|eighth|ninth|tenth)[-\s]+place|leading|highest|lowest|"
    r"top[-\s]+(?:project|build|entry)|best[-\s]+(?:project|build|entry)|perfect\s+ten)\b"
    r"|\b\d+(?:\.\d+)?\s*(?:/|out\s+of|of)\s*\d+\b"
    r"|(?:#\s*\d+|\bnumber\s+(?:one|two|three|four|five|six|seven|eight|nine|ten)\b)"
    r"|\b(?:\d+(?:\.\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"\s+points?\b"
    r"|\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"(?:\s+point\s+(?:zero|one|two|three|four|five|six|seven|eight|nine))?"
    r"\s+out\s+of\s+"
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)
_PROGRESS_STATUSES = frozenset({"running", "complete", "failed"})
_PROGRESS_STAGES = frozenset(
    {
        "shadow-spec",
        "public-scoring",
        "shadow-analysis",
        "ranking-seal",
        "verdicts",
        "feedback",
        "complete",
    }
)
_ISO_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def redact_audience_narrative(value: Any, fallback: str) -> str:
    """Keep audience narrative from leaking scores through model-written text."""
    text = " ".join(str(value or "").split())
    if not text or _NARRATIVE_SPOILER_RE.search(text):
        return fallback
    return text


def _load_json_or_none(path: Path) -> Optional[Any]:
    """Load JSON from ``path``. Returns ``None`` when the artifact is absent.

    A missing optional artifact is an intentional, expected state (not every
    bundle has been judged, sealed, or awarded yet) so it is represented as
    ``None`` rather than swallowed inside a broad try/except. Malformed JSON
    in a file that does exist is a real error and is left to raise.
    """
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _load_json_dir(dir_path: Path) -> List[Dict[str, Any]]:
    """Load every ``*.json`` file directly under ``dir_path``, sorted by
    filename. Returns ``[]`` when the directory doesn't exist yet — an
    intentional empty value for optional artifact collections."""
    if not dir_path.exists():
        return []
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(dir_path.glob("*.json"))
    ]


def _redact_verdict(verdict: Dict[str, Any]) -> Dict[str, Any]:
    """Return an audience-safe verdict without model-derived intake echoes."""
    redacted = {
        field: verdict[field]
        for field in ("submission_id", "project_name", "builder_name", "verdict_at")
        if field in verdict
    }
    source_reactions = verdict.get("archetype_verdicts")
    if isinstance(source_reactions, list):
        redacted["archetype_verdicts"] = [
            {
                "archetype_id": reaction.get("archetype_id", ""),
                "archetype_name": reaction.get("archetype_name", "Panel"),
                "perspective": "The panel found a thoughtful detail worth celebrating.",
                "bright_spot": "This project gave the panel a thoughtful detail to celebrate.",
            }
            if isinstance(reaction, dict)
            else {
                "archetype_id": "",
                "archetype_name": "Panel",
                "perspective": "The panel found a thoughtful detail worth celebrating.",
                "bright_spot": "This project gave the panel a thoughtful detail to celebrate.",
            }
            for reaction in source_reactions
        ]
    return redacted


def _redact_submission(submission: Dict[str, Any]) -> Dict[str, Any]:
    """Hide detailed builder intake from the shared screen until awards."""
    redacted = dict(submission)
    for field in _PRIVATE_PROJECT_CONTEXT_FIELDS:
        redacted.pop(field, None)
    return redacted


def _safe_progress_int(value: Any, maximum: int) -> int:
    """Return bounded telemetry counts without coercing untrusted values."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(value, maximum))


def _audience_safe_progress(progress: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Whitelist live telemetry so a malformed progress file cannot leak results."""
    if not isinstance(progress, dict):
        return None
    submissions = progress.get("submissions", {})
    if not isinstance(submissions, dict):
        submissions = {}
    total = _safe_progress_int(submissions.get("total"), 100_000)
    completed = min(
        _safe_progress_int(submissions.get("completed"), total),
        total,
    )
    status = progress.get("status")
    stage = progress.get("stage")
    updated_at = progress.get("updated_at")
    safe: Dict[str, Any] = {
        "schema_version": "1.0",
        "status": status if status in _PROGRESS_STATUSES else "pending",
        "stage": stage if stage in _PROGRESS_STAGES else "pending",
        "submissions": {
            "completed": completed,
            "total": total,
        },
        "max_parallel_calls": _safe_progress_int(
            progress.get("max_parallel_calls"), 32
        ),
        "remaining_model_calls": _safe_progress_int(
            progress.get("remaining_model_calls"), 1_000_000
        ),
    }
    if isinstance(updated_at, str) and _ISO_TIMESTAMP_RE.fullmatch(updated_at):
        safe["updated_at"] = updated_at
    if "estimated_remaining_seconds" in progress:
        safe["estimated_remaining_seconds"] = _safe_progress_int(
            progress.get("estimated_remaining_seconds"), 3_600
        )
    return safe


def _redact_feedback_assessment(value: Any, fallback: str) -> Any:
    """Hide model-authored assessment text until the audience reveal."""
    if not isinstance(value, dict):
        return {}
    redacted = {
        field: value[field]
        for field in ("status", "source")
        if field in value
    }
    for field in ("summary", "evidence"):
        if field in value:
            redacted[field] = fallback
    return redacted


def _redact_feedback(feedback: Dict[str, Any]) -> Dict[str, Any]:
    """Return feedback safe for an unrevealed audience projection."""
    redacted = {
        field: feedback[field]
        for field in (
            "submission_id",
            "builder_name",
            "project_name",
            "tone_checked",
            "delivered_at",
        )
        if field in feedback
    }
    redacted["bright_spot"] = "This project brought a thoughtful moment to the room."
    redacted["next_commit"] = "A helpful next step will be shared after the reveal."
    redacted["panel_notes"] = "The panel has a supportive note ready for this project."
    highlights = feedback.get("judges_liked")
    if isinstance(highlights, list):
        redacted["judges_liked"] = [
            {
                "lens": "Panel lens",
                "highlight": "The panel found a thoughtful detail worth celebrating.",
            }
            if isinstance(highlight, dict)
            else {
                "lens": "Panel lens",
                "highlight": "The panel found a thoughtful detail worth celebrating.",
            }
            for highlight in highlights
        ]
    if "copilot_use" in feedback:
        redacted["copilot_use"] = _redact_feedback_assessment(
            feedback.get("copilot_use"),
            "Copilot-use context will be shared after the reveal.",
        )
    if "innovation_signal" in feedback:
        redacted["innovation_signal"] = _redact_feedback_assessment(
            feedback.get("innovation_signal"),
            "Innovation context will be shared after the reveal.",
        )
    if "frontier_use" in feedback:
        redacted["frontier_use"] = _redact_feedback_assessment(
            feedback.get("frontier_use"),
            "Frontier-use context will be shared after the reveal.",
        )
    grounding = feedback.get("grounding")
    if isinstance(grounding, dict):
        sources = grounding.get("sources")
        redacted["grounding"] = {
            "status": grounding.get("status", "pending"),
            "policy": "Project-context evidence references will be shared after the reveal.",
            "source_count": len(sources) if isinstance(sources, list) else 0,
        }
    for field, fallback in (
        (
            "copilot_next_moves",
            "A Copilot improvement idea will be shared after the reveal.",
        ),
        (
            "frontier_experiments",
            "A frontier experiment idea will be shared after the reveal.",
        ),
    ):
        values = feedback.get(field)
        if isinstance(values, list):
            redacted[field] = [fallback for _ in values]
    return redacted


def _arrival_key(record: Dict[str, Any]) -> Any:
    """Stable, non-ranking sort key: chronological arrival, falling back to
    submission_id for a deterministic (but still score-blind) order."""
    return (record.get("submitted_at") or "", record.get("submission_id") or "")


@dataclass
class BundleView:
    """Normalized, immutable snapshot of a bundle produced by a
    :class:`BundleReader` projection."""

    run_id: str
    event_name: str
    status: str
    mode: str
    revealed: bool
    audience_safe: bool
    submissions: List[Dict[str, Any]] = field(default_factory=list)
    verdicts: List[Dict[str, Any]] = field(default_factory=list)
    feedback: List[Dict[str, Any]] = field(default_factory=list)
    shadow_score: Optional[Dict[str, Any]] = None
    shadow_spec: Optional[Dict[str, Any]] = None
    shadow_assessment: Optional[Dict[str, Any]] = None
    awards: Optional[Dict[str, Any]] = None
    freshness_gate: Optional[Dict[str, Any]] = None
    evaluation_progress: Optional[Dict[str, Any]] = None
    command_log: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def verdict_map(self) -> Dict[str, Dict[str, Any]]:
        return {v.get("submission_id"): v for v in self.verdicts}

    @property
    def feedback_map(self) -> Dict[str, Dict[str, Any]]:
        return {f.get("submission_id"): f for f in self.feedback}


class BundleReader:
    """Read-only accessor for a single Hackathon Judge run bundle.

    Every accessor re-reads its artifact from disk on each call — the
    reader intentionally holds no cache, so callers that poll a live bundle
    (e.g. a dashboard) always see the current on-disk state.
    """

    def __init__(self, bundle_path: Union[str, Path]):
        self.bundle_path = Path(bundle_path)

    # -- raw artifact accessors ---------------------------------------------

    def manifest(self) -> Dict[str, Any]:
        return _load_json_or_none(self.bundle_path / "manifest" / "bundle.json") or {}

    def status(self) -> str:
        return self.manifest().get("status", "unknown")

    def mode(self) -> str:
        return self.manifest().get("mode", "workshop")

    def run_id(self) -> str:
        return self.manifest().get("run_id", self.bundle_path.name)

    def event_name(self) -> str:
        event = self.manifest().get("event", {})
        if isinstance(event, dict) and event.get("name"):
            return str(event["name"])
        return "Hackathon Judge"

    def command_log(self) -> List[Dict[str, Any]]:
        """command_log is embedded directly in the manifest, not a
        separate artifact."""
        return list(self.manifest().get("command_log", []))

    def submissions(self) -> List[Dict[str, Any]]:
        return _load_json_dir(self.bundle_path / "inputs")

    def verdicts(self) -> List[Dict[str, Any]]:
        return _load_json_dir(self.bundle_path / "verdicts")

    def feedback(self) -> List[Dict[str, Any]]:
        return _load_json_dir(self.bundle_path / "feedback")

    def shadow_score(self) -> Optional[Dict[str, Any]]:
        return _load_json_or_none(self.bundle_path / "sealed" / "shadow_score.json")

    def shadow_spec(self) -> Optional[Dict[str, Any]]:
        return _load_json_or_none(self.bundle_path / "sealed" / "shadow_spec.json")

    def shadow_assessment(self) -> Optional[Dict[str, Any]]:
        return _load_json_or_none(
            self.bundle_path / "sealed" / "shadow_assessment.json"
        )

    def awards(self) -> Optional[Dict[str, Any]]:
        return _load_json_or_none(self.bundle_path / "winner" / "awards.json")

    def freshness_gate(self) -> Optional[Dict[str, Any]]:
        return _load_json_or_none(self.bundle_path / "freshness_gate.json")

    def evaluation_progress(self) -> Optional[Dict[str, Any]]:
        return _load_json_or_none(self.bundle_path / "eval" / "progress.json")

    # -- derived state --------------------------------------------------------

    def is_revealed(self) -> bool:
        """True once the manifest status has reached ``awarded`` or
        ``exported`` — the point at which score data is safe to display."""
        return self.status() in REVEALED_STATUSES

    # -- projections ------------------------------------------------------

    def operator_view(self) -> BundleView:
        """Full-fidelity snapshot with every score preserved. For
        facilitators/operators only — never present this to a live
        audience before a reveal."""
        return BundleView(
            run_id=self.run_id(),
            event_name=self.event_name(),
            status=self.status(),
            mode=self.mode(),
            revealed=self.is_revealed(),
            audience_safe=False,
            submissions=self.submissions(),
            verdicts=self.verdicts(),
            feedback=self.feedback(),
            shadow_score=self.shadow_score(),
            shadow_spec=self.shadow_spec(),
            shadow_assessment=self.shadow_assessment(),
            awards=self.awards(),
            freshness_gate=self.freshness_gate(),
            evaluation_progress=self.evaluation_progress(),
            command_log=self.command_log(),
        )

    def audience_view(self) -> BundleView:
        """Safe projection for live display.

        - ``total_score``/``dimension_scores`` are stripped from every
          verdict until the manifest status is ``awarded`` or ``exported``.
        - The shadow score vault (which contains the full ranking) is
          withheld until revealed.
        - Awards are withheld until revealed (they are declared before the
          ``award`` command flips status, so they could otherwise leak the
          winner early).
        - Submissions and verdicts are always ordered by arrival
          (submission timestamp), never by score or rank, so this
          projection can never be used to reconstruct a leaderboard.
        """
        revealed = self.is_revealed()
        verdicts = self.verdicts()
        feedback = self.feedback()
        submissions = self.submissions()
        progress = _audience_safe_progress(self.evaluation_progress())
        if not revealed:
            submissions = [_redact_submission(s) for s in submissions]
            verdicts = [_redact_verdict(v) for v in verdicts]
            feedback = [_redact_feedback(f) for f in feedback]

        submissions = sorted(submissions, key=_arrival_key)
        arrival_index = {
            sub.get("submission_id"): i for i, sub in enumerate(submissions)
        }
        verdicts = sorted(
            verdicts,
            key=lambda v: (
                arrival_index.get(v.get("submission_id"), len(arrival_index)),
                v.get("submission_id") or "",
            ),
        )
        feedback = sorted(
            feedback,
            key=lambda f: (
                arrival_index.get(f.get("submission_id"), len(arrival_index)),
                f.get("submission_id") or "",
            ),
        )

        return BundleView(
            run_id=self.run_id(),
            event_name=self.event_name(),
            status=self.status(),
            mode=self.mode(),
            revealed=revealed,
            audience_safe=True,
            submissions=submissions,
            verdicts=verdicts,
            feedback=feedback,
            shadow_score=self.shadow_score() if revealed else None,
            shadow_spec=self.shadow_spec() if revealed else None,
            shadow_assessment=self.shadow_assessment() if revealed else None,
            awards=self.awards() if revealed else None,
            freshness_gate=self.freshness_gate(),
            evaluation_progress=progress,
            command_log=self.command_log(),
        )
