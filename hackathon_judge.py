#!/usr/bin/env python3
"""
Hackathon Judge — sealed, replayable judging for project events.
Architecture: the run bundle is the canonical unit of record.

Commands: workshop, init, submit, quick, judge, present, replay, resume,
          compare, list, award, feedback, export, validate, doctor

Exit codes:
  0  — success
  1  — unhandled exception
  2  — BundleSealError (write-once violation)
  3  — FreshnessGateBlock (stale model, strict mode)
  4  — ToneSafetyFailure (banned phrase / missing required element)
  5  — BundleTamperError (hash mismatch in validate)
  6  — SubmissionSizeError (input exceeds cap)
  7  — ConfigValidationError (rubric weights != 1.0, etc.)
  8  — ModelAPIError (API call failure)
  9  — HumanApprovalGate (export blocked; winner card not approved)
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
import copy
from contextvars import ContextVar
from functools import wraps
import hashlib
import importlib
import importlib.metadata
import importlib.util
import io
import json
import math
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Sequence

from event_spec import (
    DEFAULT_EVENT_SPEC,
    EventSpecValidationError,
    event_spec_to_rubric,
    legacy_rubric_to_event_spec,
    resolve_event_spec,
)
# ---------------------------------------------------------------------------
# Layer 0 — Constants and defaults
# ---------------------------------------------------------------------------

VERSION = "3.0.0"
AWARD_SLATE = copy.deepcopy(DEFAULT_EVENT_SPEC["awards"])
AWARD_NAME = next(
    award["name"] for award in AWARD_SLATE if award["id"] == "grand-prize"
)
DEFAULT_REGISTRY_PATH = Path.home() / ".hackathon_judge" / "registry" / "log.ndjson"
DEFAULT_RUNS_DIR = Path.home() / ".hackathon_judge" / "runs"
SCHEMA_VERSION = "1.0"
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

MAX_SUBMISSION_SIZE_DEFAULT = 5 * 1024 * 1024  # 5 MiB
# Reserve most of the two-minute show for reading and model work; animation
# itself must never turn a short ceremony into a long wait.
SHOWTIME_PAUSE_BUDGET_SECONDS = 18.0
DEMO_TIME_BUDGET_SECONDS = 120.0

DEMO_SUBMISSIONS = [
    {
        "url": "https://github.com/demo-day/pulseboard",
        "builder_name": "Team Aurora",
        "copilot_evidence": (
            "Used Copilot to turn interview notes into the first event-status workflow "
            "and its acceptance tests."
        ),
        "frontier_evidence": (
            "Prototyped a human-reviewed agent that summarizes live event changes."
        ),
        "problem_statement": (
            "Event teams lose time reconciling project status across scattered updates."
        ),
        "intended_user": "hackathon organizers and demo-day producers",
        "demo_url": "https://demo.example/pulseboard",
        "builder_notes": "A single live board turns updates into a clear run of show.",
    },
    {
        "url": "https://github.com/demo-day/fieldnote",
        "builder_name": "Team Northstar",
        "copilot_evidence": (
            "Used Copilot to scaffold the mobile capture flow and draft edge-case tests."
        ),
        "frontier_evidence": (
            "Tested a multimodal workflow that converts field photos and notes into "
            "review-ready summaries."
        ),
        "problem_statement": (
            "Field teams spend too much time rebuilding context after customer visits."
        ),
        "intended_user": "customer success and field engineering teams",
        "demo_url": "https://demo.example/fieldnote",
        "builder_notes": "Capture once, review quickly, and keep a human in control.",
    },
    {
        "url": "https://github.com/demo-day/skillbridge",
        "builder_name": "Team Lift",
        "copilot_evidence": (
            "Used Copilot to draft the matching service and refine onboarding copy."
        ),
        "frontier_evidence": (
            "Built a bounded recommendation experiment with explicit mentor approval."
        ),
        "problem_statement": (
            "New contributors struggle to find a first project that matches their skills."
        ),
        "intended_user": "open source newcomers and volunteer maintainers",
        "demo_url": "https://demo.example/skillbridge",
        "builder_notes": "The demo matches one contributor to one achievable next step.",
    },
]

DEMO_REPO_METADATA = {
    "https://github.com/demo-day/pulseboard": {
        "name_with_owner": "demo-day/pulseboard",
        "description": "A live command center for project showcases and event operations.",
        "language": "TypeScript",
        "stars": 128,
        "forks": 14,
        "updated_at": "2026-07-21T18:00:00Z",
        "pushed_at": "2026-07-21T18:00:00Z",
        "topics": ["events", "realtime", "dashboard"],
        "homepage": "https://demo.example/pulseboard",
        "url": "https://github.com/demo-day/pulseboard",
        "source": "bundled-demo",
    },
    "https://github.com/demo-day/fieldnote": {
        "name_with_owner": "demo-day/fieldnote",
        "description": "A multimodal field-note workflow with human-reviewed summaries.",
        "language": "Python",
        "stars": 94,
        "forks": 9,
        "updated_at": "2026-07-21T17:00:00Z",
        "pushed_at": "2026-07-21T17:00:00Z",
        "topics": ["multimodal", "field-work", "human-review"],
        "homepage": "https://demo.example/fieldnote",
        "url": "https://github.com/demo-day/fieldnote",
        "source": "bundled-demo",
    },
    "https://github.com/demo-day/skillbridge": {
        "name_with_owner": "demo-day/skillbridge",
        "description": "A guided first-contribution matcher for open source communities.",
        "language": "Go",
        "stars": 76,
        "forks": 11,
        "updated_at": "2026-07-21T16:00:00Z",
        "pushed_at": "2026-07-21T16:00:00Z",
        "topics": ["open-source", "mentorship", "recommendations"],
        "homepage": "https://demo.example/skillbridge",
        "url": "https://github.com/demo-day/skillbridge",
        "source": "bundled-demo",
    },
}

AUDIENCE_REVEAL_MOMENTS = (
    {
        "cue": "Hands on knees—give this final envelope your loudest drumroll.",
        "confirm": "Is the room rolling?",
        "payoff": "That drumroll has reached the stage. Open the envelope.",
    },
    {
        "cue": "Applause meter check—show these builders how loud this room can get.",
        "confirm": "Is the applause up?",
        "payoff": "The room is officially loud enough. Bring on the result.",
    },
    {
        "cue": "Five fingers up. Count the final five seconds together.",
        "confirm": "Is everybody counting?",
        "payoff": "Countdown confirmed. The final result is ready.",
    },
    {
        "cue": "Strike your best champion pose and hold it for the reveal.",
        "confirm": "Are the victory poses locked?",
        "payoff": "Champion energy confirmed. Open the envelope.",
    },
    {
        "cue": "On three, shout the name of a build that surprised you tonight.",
        "confirm": "Did the room make some noise?",
        "payoff": "The builders heard you. Now for the final result.",
    },
    {
        "cue": "Nobody move. Nobody blink. Give this envelope a full suspense freeze.",
        "confirm": "Is the room frozen?",
        "payoff": "Perfect freeze. Break the suspense.",
    },
    {
        "cue": "Give me your most dramatic collective gasp—practice it once.",
        "confirm": "Did the gasp land?",
        "payoff": "Drama level confirmed. Reveal the result.",
    },
    {
        "cue": "Start a table tap—quiet first, then build it into thunder.",
        "confirm": "Is the thunder building?",
        "payoff": "The floor is rumbling. Time for the reveal.",
    },
    {
        "cue": "If something inspired you tonight, put both hands in the air.",
        "confirm": "Are the hands up?",
        "payoff": "That is a room full of inspiration. Open the envelope.",
    },
    {
        "cue": (
            "When the winner appears, celebrate like this build just shipped to "
            "a million people."
        ),
        "confirm": "Is everybody ready for the joy eruption?",
        "payoff": "Joy is armed. Reveal the final result.",
    },
)

# Tone Safety — banned phrase categories (lowercase for matching)
BANNED_TEARDOWN = [
    "failed to", "disappointing", "lacks", "weak", "poor", "mediocre",
    "mistake", "terrible", "awful", "bad", "horrible", "inadequate",
    "insufficient", "pathetic", "worthless", "useless", "subpar",
]
BANNED_DISMISSIVE = [
    "just a", "only a", "merely", "basic attempt", "simple mistake",
    "nothing special", "nothing impressive",
]
BANNED_NEGATIVE_FRAMING = [
    "unfortunately,", "sadly,", "regrettably,", "however, this",
    "this fails", "this missed",
]

# Required positive framing keywords for bright_spot
BRIGHT_SPOT_KEYWORDS = [
    "great", "excellent", "strong", "impressive", "built", "achieved",
    "brilliant", "creative", "innovative", "outstanding", "remarkable",
    "fantastic", "well done", "solid", "powerful", "elegant", "thoughtful",
    "insightful", "effective", "clear", "demonstrates", "showcases",
    "highlights", "delivers", "shows", "proves", "demonstrates",
]

# Forward-looking verb patterns for next_commit
FORWARD_NUDGE_PATTERNS = [
    r"\bconsider\b", r"\badd\b", r"\bexplore\b", r"\btry\b", r"\bbuild\b",
    r"\bextend\b", r"\bimprove\b", r"\brefine\b", r"\bexpand\b", r"\bcreate\b",
    r"\bintegrate\b", r"\bconnect\b", r"\bleverage\b", r"\benhance\b",
    r"\boptimize\b", r"\bship\b", r"\blaunch\b", r"\btest\b", r"\bdeploy\b",
    r"\bnext\b", r"\bfuture\b", r"\byour next\b", r"\bcould\b", r"\bwould\b",
]

# Default rubric configuration. New event packs resolve to this shape before
# reaching the existing evaluation engine, so historic bundle readers stay valid.
DEFAULT_RUBRIC = event_spec_to_rubric(DEFAULT_EVENT_SPEC)

# ---------------------------------------------------------------------------
# Console Experience — colorful, deterministic, artifact-safe
# ---------------------------------------------------------------------------

ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "gold": "\033[38;5;220m",
}

class _ShowtimePacer:
    """Caps intentional pauses without affecting evaluation runtime."""

    def __init__(self, budget_seconds: float):
        self.remaining_seconds = max(0.0, budget_seconds)

    def take(self, requested_seconds: float) -> float:
        delay = min(max(0.0, requested_seconds), self.remaining_seconds)
        self.remaining_seconds -= delay
        return delay


_SHOWTIME_PACER: ContextVar[Optional[_ShowtimePacer]] = ContextVar(
    "hackathon_judge_showtime_pacer",
    default=None,
)


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("HJ_NO_COLOR"):
        return False
    if os.environ.get("HJ_COLOR", "").lower() == "always":
        return True
    return sys.stdout.isatty()


def _paint(text: str, color: str = "reset", *, bold: bool = False) -> str:
    if not _color_enabled():
        return text
    prefix = ""
    if bold:
        prefix += ANSI["bold"]
    prefix += ANSI.get(color, "")
    return f"{prefix}{text}{ANSI['reset']}"


def _terminal_width(min_width: int = 54, max_width: int = 88) -> int:
    try:
        return min(max(min_width, os.get_terminal_size().columns - 4), max_width)
    except OSError:
        return 72


def _truncate(text: str, width: int) -> str:
    text = str(text or "")
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def _score_bar(score: float, maximum: float = 10.0, width: int = 18) -> str:
    ratio = 0.0 if maximum <= 0 else max(0.0, min(1.0, score / maximum))
    filled = round(ratio * width)
    bar = "█" * filled + "░" * (width - filled)
    color = "green" if ratio >= 0.8 else "yellow" if ratio >= 0.6 else "red"
    return _paint(bar, color)


def _set_terminal_title(title: str) -> None:
    """Set a safe terminal title when output is attached to a real TTY."""
    if not sys.stdout.isatty():
        return
    safe_title = re.sub(r"[\x00-\x1f\x7f]", "", title)
    print(f"\033]0;{safe_title}\007", end="", flush=True)


def _magic_banner(title: str, subtitle: str = "") -> None:
    title_line = f"✨  {title}  ✨"
    terminal_width = _terminal_width()
    width = min(terminal_width, max(66, len(title_line) + 6, len(subtitle) + 6))
    print()
    print(_paint("╔" + "═" * width + "╗", "magenta", bold=True))
    print(_paint("║" + title_line.center(width) + "║", "magenta", bold=True))
    if subtitle:
        print(_paint("║" + subtitle.center(width) + "║", "cyan"))
    print(_paint("╚" + "═" * width + "╝", "magenta", bold=True))


def _sideline(message: str, icon: str = "🎙️", color: str = "cyan") -> None:
    print(_paint(f"{icon} {message}", color, bold=True))


def _step(step: int, total: int, message: str, icon: str = "⬢") -> None:
    print(_paint(f"  {icon} [{step}/{total}] {message}", "cyan"))


def _drumroll(message: str = "The panel is ready to reveal its pick.",
              args: Optional[argparse.Namespace] = None) -> None:
    live = _suspense_enabled(args)
    print(_paint("🥁 ...", "yellow", bold=True))
    if live:
        _showtime_pause(args, 0.45)
    print(_paint("🥁🥁 ...", "yellow", bold=True))
    if live:
        _showtime_pause(args, 0.45)
    print(_paint(f"🥁🥁🥁 {message}", "gold", bold=True))
    if live:
        _showtime_pause(args, 0.45)


def _success(message: str) -> None:
    print(_paint(f"✅ {message}", "green", bold=True))


def _warning(message: str) -> None:
    print(_paint(f"⚠️  {message}", "yellow", bold=True), file=sys.stderr)


def _showtime_enabled(args: Optional[argparse.Namespace] = None) -> bool:
    if os.environ.get("HJ_SHOWTIME", "").lower() in {"1", "true", "yes", "on"}:
        return True
    return bool(getattr(args, "showtime", False)) if args is not None else False


def _showtime_pause(args: Optional[argparse.Namespace] = None, seconds: float = 0.7) -> None:
    if not _suspense_enabled(args):
        return
    pacer = _SHOWTIME_PACER.get()
    delay = pacer.take(seconds) if pacer else seconds
    if delay:
        time.sleep(delay)


def _suspense_enabled(args: Optional[argparse.Namespace] = None) -> bool:
    if not _showtime_enabled(args):
        return False
    if getattr(args, "no_suspense", False):
        return False
    return sys.stdout.isatty() or os.environ.get("HJ_COLOR", "").lower() == "always"


def _with_showtime_pacing(is_live: Optional[Callable[[argparse.Namespace], bool]] = None) -> Callable:
    """Apply one animation budget to a top-level live command and its children."""

    def decorate(func: Callable) -> Callable:
        @wraps(func)
        def wrapped(args: argparse.Namespace, *extra: Any, **kwargs: Any) -> Any:
            if _SHOWTIME_PACER.get() is not None:
                return func(args, *extra, **kwargs)
            enabled = is_live(args) if is_live else _showtime_enabled(args)
            if not enabled:
                return func(args, *extra, **kwargs)
            token = _SHOWTIME_PACER.set(_ShowtimePacer(SHOWTIME_PAUSE_BUDGET_SECONDS))
            try:
                return func(args, *extra, **kwargs)
            finally:
                _SHOWTIME_PACER.reset(token)

        return wrapped

    return decorate


def _workshop_showtime_enabled(args: argparse.Namespace) -> bool:
    return (
        bool(getattr(args, "showtime", False))
        or bool(getattr(args, "demo", False))
        or bool(getattr(args, "projector", False))
        or bool(getattr(args, "require_live_terminal", False))
        or bool(getattr(args, "require_projector_window", False))
        or not bool(getattr(args, "configure", False))
    )


def _present_showtime_enabled(args: argparse.Namespace) -> bool:
    return _showtime_enabled(args) or bool(getattr(args, "projector", False))


def _panel_style_for(manifest: Dict) -> str:
    choices = manifest.get("workshop_choices", {})
    if isinstance(choices, dict) and choices.get("panel_style") == "professional":
        return "professional"
    return "fun"


def _natural_join(items: List[str]) -> str:
    if not items:
        return "the panel"
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def _panel_lens_names(event_spec: Dict) -> List[str]:
    return [
        _truncate(str(lens.get("name", "Panel lens")), 28)
        for lens in event_spec.get("review_lenses", [])[:3]
    ]


def _audience_safe_commentary(value: Any, fallback: str) -> str:
    """Allow useful project narration while redacting result-like language."""
    from bundle_reader import redact_audience_narrative

    return redact_audience_narrative(value, fallback)


def _panel_opening_message(event_spec: Dict, panel_style: str) -> str:
    lenses = _natural_join(_panel_lens_names(event_spec))
    if panel_style == "professional":
        return f"Panel brief: {lenses} will review every project independently. Scores stay sealed."
    return f"Panel chatter: {lenses} are in the huddle. No spoilers, just good builds."


def _panel_progress_message(index: int, total: int, panel_style: str) -> str:
    if index == total:
        return (
            "Final review sealed. The panel has its notes; the room gets the spotlights next."
            if panel_style == "professional"
            else "Final review sealed. The panel has notes, the room gets spotlights. No peeking."
        )
    if index == 1:
        return (
            "First review sealed. Every project gets the same careful look."
            if panel_style == "professional"
            else "First review sealed. Fresh eyes, clean slate, no leaderboard."
        )
    return (
        "Halfway through. The panel is still reviewing every project on its own merits."
        if panel_style == "professional"
        else "Halfway through. The room is cooking; the envelope stays shut."
    )


def _showcase_milestones(total: int) -> set[int]:
    if total <= 0:
        return set()
    milestones = {1, total}
    if total >= 4:
        milestones.add((total + 1) // 2)
    return milestones


def _live_panel_take(verdict: Dict, panel_style: str, audience_locked: bool) -> tuple[str, str]:
    """Pick one deterministic, score-safe stored reaction for a spotlight."""
    reactions = verdict.get("archetype_verdicts", [])
    if not reactions:
        return "Panel", "This project gave the panel a thoughtful detail to celebrate."

    seed = f"{verdict.get('submission_id', '')}:{panel_style}".encode("utf-8")
    index = int(hashlib.sha256(seed).hexdigest(), 16) % len(reactions)
    reaction = reactions[index]
    lens = _truncate(str(reaction.get("archetype_name", "Panel")), 28)
    fallback = "This project gave the panel a thoughtful detail to celebrate."
    take = reaction.get("bright_spot") or reaction.get("perspective") or fallback
    if audience_locked:
        take = _audience_safe_commentary(take, fallback)
    return lens, _truncate(str(take), 116)


def _act_break(label: str, args: Optional[argparse.Namespace] = None) -> None:
    if not _showtime_enabled(args):
        return
    width = min(76, _terminal_width(max_width=80))
    print()
    print(_paint("━" * width, "blue", bold=True))
    print(_paint(f"  ▸ {label}", "magenta", bold=True))
    print(_paint("━" * width, "blue", bold=True))
    print()
    _showtime_pause(args, 0.35)


def _tonight_card(run_id: str, repo_count: int, awards: str,
                  args: Optional[argparse.Namespace] = None) -> None:
    if not _showtime_enabled(args):
        return
    width = min(70, _terminal_width(max_width=76))
    award_labels = " · ".join(a.strip() for a in awards.split(",") if a.strip())
    lines = [
        ("Run", run_id),
        ("Projects entered", str(repo_count)),
        ("Awards on offer", award_labels),
        ("Mode", "Showtime Autopilot"),
        ("Envelope", "sealed live, replayable forever"),
    ]
    print(_paint("┌" + "─" * width + "┐", "blue", bold=True))
    print(_paint("│" + "🎟  TONIGHT'S RUN".center(width) + "│", "gold", bold=True))
    for label, value in lines:
        text = f"  {label + ':':<18} {_truncate(value, width - 24)}"
        print(_paint("│" + text.ljust(width) + "│", "cyan"))
    print(_paint("│" + '"No teardowns. Only spotlights."'.center(width) + "│", "green"))
    print(_paint("└" + "─" * width + "┘", "blue", bold=True))
    _showtime_pause(args, 0.5)


def _project_count_hero(count: int, args: Optional[argparse.Namespace] = None) -> None:
    if not _showtime_enabled(args):
        return
    width = min(76, _terminal_width(max_width=80))
    noun = "PROJECT" if count == 1 else "PROJECTS"
    print()
    print(_paint(f"{count} {noun} ENTER THE SHOWCASE".center(width), "gold", bold=True))
    print()
    _showtime_pause(args, 0.45)


def _countdown_reveal(args: Optional[argparse.Namespace] = None) -> None:
    if not _suspense_enabled(args):
        _drumroll("And the award goes to...", args)
        return
    for n in (3, 2, 1):
        print(_paint(f"    {'🥁' * n}  {n}...", "yellow", bold=True))
        _showtime_pause(args, 0.75)
    print(_paint("    🥁🥁🥁🥁  AND THE AWARD GOES TO...", "gold", bold=True))
    _showtime_pause(args, 0.55)


def _audience_reveal_moment(args: Optional[argparse.Namespace] = None) -> None:
    """Stage one short, reproducible audience interaction before the final reveal."""
    if not _showtime_enabled(args):
        return
    run_id = str(getattr(args, "run_id", "") or "live-show")
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    moment = AUDIENCE_REVEAL_MOMENTS[int(digest[:8], 16) % len(AUDIENCE_REVEAL_MOMENTS)]

    _sideline("Sideline report: the final envelope is at the stage.", "📡", "magenta")
    _sideline(moment["cue"], "🙌", "gold")
    if _suspense_enabled(args) and sys.stdin.isatty():
        # This signature audience beat remains interactive even in --yes mode.
        if not _confirm(moment["confirm"]):
            try:
                input(
                    _paint(
                        "The envelope stays sealed. Press Enter when the room is ready: ",
                        "yellow",
                        bold=True,
                    )
                )
            except EOFError:
                pass
    else:
        _sideline("Audience check ready; continuing without an interactive pause.", "✅", "green")
    _sideline(moment["payoff"], "⚡", "gold")


# Known approved models (used when no API is available)
APPROVED_MODELS = [
    {"id": "claude-opus-4.7-xhigh",       "tier": 6, "reasoning": "xhigh", "premium": True, "deprecated": False},
    {"id": "claude-opus-4.7-high",        "tier": 5, "reasoning": "high",  "premium": True, "deprecated": False},
    {"id": "claude-opus-4.8",             "tier": 5, "reasoning": "high",  "premium": True, "deprecated": False},
    {"id": "claude-opus-4.7-1m-internal", "tier": 5, "reasoning": "high",  "premium": True, "deprecated": False},
    {"id": "gpt-5.6-terra",               "tier": 6, "reasoning": "xhigh", "premium": True, "deprecated": False},
    {"id": "gpt-5.5",                     "tier": 5, "reasoning": "high",  "premium": True, "deprecated": False},
    {"id": "gpt-5.4",                     "tier": 4, "reasoning": "high",  "premium": True, "deprecated": False},
    {"id": "gpt-5.3-codex",               "tier": 4, "reasoning": "high",  "premium": True, "deprecated": False},
    {"id": "gemini-3.1-pro-preview",      "tier": 4, "reasoning": "high",  "premium": True, "deprecated": False},
    # Standard/fallback entries remain available for explicit permissive tests only.
    {"id": "gpt-4o",                      "tier": 3, "reasoning": "medium", "premium": False, "deprecated": False},
    {"id": "gpt-4-turbo",                 "tier": 3, "reasoning": "medium", "premium": False, "deprecated": False},
    {"id": "gpt-4",                       "tier": 2, "reasoning": "medium", "premium": False, "deprecated": False},
    {"id": "gpt-3.5-turbo",               "tier": 1, "reasoning": "low",    "premium": False, "deprecated": False},
    {"id": "gpt-4o-mini",                 "tier": 2, "reasoning": "low",    "premium": False, "deprecated": False},
    {"id": "gpt-4-legacy",                "tier": 1, "reasoning": "low",    "premium": False, "deprecated": True},
    {"id": "gpt-3.5-legacy",              "tier": 0, "reasoning": "low",    "premium": False, "deprecated": True},
]

# ---------------------------------------------------------------------------
# Layer 1 — Exceptions
# ---------------------------------------------------------------------------

class HackathonJudgeError(Exception):
    """Base error with exit code."""
    exit_code: int = 1


class BundleSealError(HackathonJudgeError):
    exit_code = 2


class FreshnessGateBlock(HackathonJudgeError):
    exit_code = 3


class ToneSafetyFailure(HackathonJudgeError):
    exit_code = 4


class BundleTamperError(HackathonJudgeError):
    exit_code = 5


class SubmissionSizeError(HackathonJudgeError):
    exit_code = 6


class ConfigValidationError(HackathonJudgeError):
    exit_code = 7


class ModelAPIError(HackathonJudgeError):
    exit_code = 8


class HumanApprovalGate(HackathonJudgeError):
    exit_code = 9

# ---------------------------------------------------------------------------
# Layer 2 — Bundle I/O
# ---------------------------------------------------------------------------

def _now(clock: Optional[Callable[[], datetime]] = None) -> str:
    """Return ISO 8601 UTC timestamp. Injectable for tests."""
    fn = clock or (lambda: datetime.now(timezone.utc))
    return fn().isoformat()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: str) -> None:
    """Write to <path>.tmp then os.replace for atomicity (POSIX)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def write_once(path: Path, data: str) -> None:
    """Write a file exactly once. Raises BundleSealError if it already exists."""
    if path.exists():
        raise BundleSealError(
            f"Write-once violation: {path} already exists. "
            "Sealed artifacts cannot be overwritten."
        )
    _atomic_write(path, data)


def write_once_json(path: Path, obj: Any) -> None:
    write_once(path, json.dumps(obj, indent=2, default=str))


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def append_ndjson(path: Path, obj: Any) -> None:
    """Append one JSON line to an NDJSON file (append-only, no seek-to-start)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, default=str) + "\n")


def read_ndjson(path: Path) -> List[Any]:
    if not path.exists():
        return []
    lines = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                lines.append(json.loads(line))
    return lines


def collect_bundle_artifacts(bundle_path: Path) -> List[Path]:
    """Return all regular files under bundle_path, excluding HASHES and SEAL."""
    artifacts = []
    for p in sorted(bundle_path.rglob("*")):
        if p.is_file() and p.name not in ("HASHES", "SEAL") and not p.name.endswith(".tmp"):
            artifacts.append(p)
    return artifacts


def hash_artifacts(bundle_path: Path) -> Dict[str, str]:
    """Compute SHA-256 for every artifact in the bundle (excluding HASHES/SEAL)."""
    result: Dict[str, str] = {}
    for p in collect_bundle_artifacts(bundle_path):
        rel = str(p.relative_to(bundle_path))
        result[rel] = _sha256_file(p)
    return result


def write_hashes_and_seal(bundle_path: Path) -> tuple[str, str]:
    """Write HASHES and SEAL files. Returns (hashes_content, seal_hash)."""
    hashes_path = bundle_path / "HASHES"
    seal_path = bundle_path / "SEAL"

    artifact_hashes = hash_artifacts(bundle_path)
    lines = [f"{digest}  {rel_path}" for rel_path, digest in sorted(artifact_hashes.items())]
    hashes_content = "\n".join(lines) + "\n"

    write_once(hashes_path, hashes_content)

    seal_hash = _sha256_bytes(hashes_content.encode("utf-8"))
    write_once(seal_path, seal_hash + "\n")
    return hashes_content, seal_hash


def _resume_partial_seal(bundle_path: Path) -> tuple[bool, str]:
    """Finish a HASHES-only export if every live artifact still matches."""
    hashes_path = bundle_path / "HASHES"
    seal_path = bundle_path / "SEAL"
    if not hashes_path.exists() or seal_path.exists():
        return False, "partial seal state was not found"

    hashes_content = hashes_path.read_text(encoding="utf-8")
    stored_hashes: Dict[str, str] = {}
    for line in hashes_content.strip().splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2:
            return False, f"HASHES contains an invalid line: {line}"
        digest, rel_path = parts
        if rel_path in stored_hashes:
            return False, f"HASHES contains a duplicate path: {rel_path}"
        stored_hashes[rel_path] = digest

    if stored_hashes != hash_artifacts(bundle_path):
        return False, "live artifacts no longer match the partial HASHES file"

    seal_hash = _sha256_bytes(hashes_content.encode("utf-8"))
    try:
        write_once(seal_path, seal_hash + "\n")
    except (BundleSealError, OSError) as exc:
        return False, str(exc)
    return True, seal_hash


def validate_run_id(run_id: str) -> str:
    """Validate a run ID before it is joined to the configured runs directory."""
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ConfigValidationError(
            "Run IDs may contain only letters, numbers, dots, underscores, and hyphens "
            "and must begin with a letter or number."
        )
    if run_id in {".", ".."}:
        raise ConfigValidationError("Run ID may not be '.' or '..'.")
    return run_id


def get_runs_dir() -> Path:
    """Return the configured root for Hackathon Judge run bundles."""
    return Path(os.environ.get("HJ_RUNS_DIR", str(DEFAULT_RUNS_DIR)))


def get_bundle_path(run_id: str, runs_dir: Optional[Path] = None) -> Path:
    """Resolve a validated run ID to a path contained by the runs directory."""
    base = (runs_dir or get_runs_dir()).resolve()
    candidate = (base / validate_run_id(run_id)).resolve()
    if candidate.parent != base:
        raise ConfigValidationError(f"Run ID '{run_id}' resolves outside the runs directory.")
    return candidate


def get_registry_path() -> Path:
    return Path(os.environ.get("HJ_REGISTRY_PATH", str(DEFAULT_REGISTRY_PATH)))


def _textual_status() -> tuple[bool, str]:
    """Return whether the tested Textual major version is importable."""
    if importlib.util.find_spec("textual") is None:
        return False, "Textual is not installed"
    try:
        textual_version = importlib.metadata.version("textual")
        major_version = int(textual_version.split(".", 1)[0])
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return False, "Textual is importable but its installed version could not be verified"
    if major_version != 8:
        return False, f"Textual {textual_version} is unsupported; install textual>=8,<9"
    try:
        importlib.import_module("textual")
        dashboard_module = importlib.import_module("hackathon_judge_dashboard")
        getattr(dashboard_module, "BuilderDashboard")
    except (ImportError, AttributeError, RuntimeError, TypeError, OSError) as exc:
        return False, f"Textual {textual_version} could not load the dashboard: {exc}"
    return True, f"Textual {textual_version}"


_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:[/?#][^\s,)]*)?",
    re.IGNORECASE,
)
_OWNER_REPO_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?![A-Za-z0-9_.-])"
)


def parse_submission_urls(raw: str) -> List[str]:
    """Extract GitHub repository URLs or owner/repo entries from pasted text."""
    found: List[str] = []
    seen = set()

    def add(owner: str, repo: str) -> None:
        repo = repo.removesuffix(".git")
        url = f"https://github.com/{owner}/{repo}"
        key = url.lower()
        if key not in seen:
            seen.add(key)
            found.append(url)

    for match in _GITHUB_URL_RE.finditer(raw or ""):
        add(match.group(1), match.group(2))

    scrubbed = _GITHUB_URL_RE.sub(" ", raw or "")
    for match in _OWNER_REPO_RE.finditer(scrubbed):
        owner, repo = match.group(1), match.group(2)
        if owner.lower() in {"http:", "https:"}:
            continue
        add(owner, repo)
    return found


def parse_submission_entries(raw: str) -> List[Dict[str, str]]:
    """
    Parse line-oriented project intake with optional attribution, evidence, and
    project context.

    Each line may use this compact form:
    ``GitHub URL | Team | Copilot evidence | Frontier evidence | Problem |
    Intended user | Demo or artifact | Builder notes``.
    Plain URLs and ``owner/repo`` entries remain fully supported.
    """
    entries: List[Dict[str, str]] = []
    seen = set()

    for line in (raw or "").splitlines():
        fields = [field.strip() for field in line.split("|")]
        urls = parse_submission_urls(fields[0] if fields else "")
        if not urls and len(fields) == 1:
            urls = parse_submission_urls(line)

        for url in urls:
            key = url.lower()
            if key in seen:
                continue
            seen.add(key)
            entry = {
                "url": url,
                "builder_name": fields[1] if len(fields) > 1 else "",
                "copilot_evidence": fields[2] if len(fields) > 2 else "",
                "frontier_evidence": fields[3] if len(fields) > 3 else "",
            }
            for index, field in enumerate(
                ("problem_statement", "intended_user", "demo_url", "builder_notes"),
                start=4,
            ):
                if len(fields) > index and fields[index]:
                    entry[field] = fields[index]
            entries.append(entry)
    return entries


def _submission_id_from_repo_url(url: str) -> str:
    owner_repo = url.replace("https://github.com/", "", 1)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", owner_repo).strip("-").lower()
    digest = hashlib.sha256(url.lower().encode("utf-8")).hexdigest()[:8]
    return f"repo-{slug}-{digest}"[:96]


def fetch_repo_metadata(url: str) -> Dict[str, Any]:
    """Best-effort GitHub metadata via gh. Never required for judging."""
    owner_repo = url.replace("https://github.com/", "", 1).strip("/")
    try:
        proc = subprocess.run(
            [
                "gh", "repo", "view", owner_repo,
                "--json",
                (
                    "nameWithOwner,description,primaryLanguage,stargazerCount,"
                    "forkCount,updatedAt,pushedAt,repositoryTopics,homepageUrl,url"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=8,
        )
        data = json.loads(proc.stdout)
        lang = data.get("primaryLanguage") or {}
        topic_records = data.get("repositoryTopics") or []
        topics = [
            topic["name"]
            for topic in topic_records
            if isinstance(topic, dict) and isinstance(topic.get("name"), str)
        ]
        return {
            "name_with_owner": data.get("nameWithOwner") or owner_repo,
            "description": data.get("description") or "",
            "language": lang.get("name") if isinstance(lang, dict) else None,
            "stars": data.get("stargazerCount"),
            "forks": data.get("forkCount"),
            "updated_at": data.get("updatedAt"),
            "pushed_at": data.get("pushedAt"),
            "topics": topics,
            "homepage": data.get("homepageUrl") or "",
            "url": data.get("url") or url,
            "source": "gh",
        }
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        return _fallback_repo_metadata(url)


def _fallback_repo_metadata(url: str) -> Dict[str, Any]:
    owner_repo = url.replace("https://github.com/", "", 1).strip("/")
    return {
        "name_with_owner": owner_repo,
        "description": "",
        "language": None,
        "stars": None,
        "forks": None,
        "updated_at": None,
        "pushed_at": None,
        "topics": [],
        "homepage": "",
        "url": url,
        "source": "fallback",
    }


def _demo_repo_metadata(url: str) -> Dict[str, Any]:
    """Return deterministic showcase metadata without network access."""
    metadata = DEMO_REPO_METADATA.get(url)
    return copy.deepcopy(metadata) if metadata else _fallback_repo_metadata(url)


def _format_count(value: Any) -> str:
    """Render public repository counts compactly for a projector card."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return str(value)
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}m"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def project_showcase_badges(metadata: Dict[str, Any]) -> List[str]:
    """Return non-scoring repository context suitable for a project spotlight."""
    badges: List[str] = []
    if metadata.get("language"):
        badges.append(f"📝 {metadata['language']}")
    if metadata.get("stars") is not None:
        badges.append(f"⭐ {_format_count(metadata['stars'])}")
    topics = metadata.get("topics")
    if isinstance(topics, list):
        topic_names = [str(topic) for topic in topics if str(topic).strip()]
        if topic_names:
            badges.append(f"🏷️ {', '.join(topic_names[:3])}")
    activity = metadata.get("pushed_at") or metadata.get("updated_at")
    if activity:
        badges.append(f"🟢 Active {str(activity)[:10]}")
    return badges


def import_url_submissions(bundle_path: Path, urls: List[Any],
                           builder_name: str = "Hackathon Participants",
                           clock: Optional[Callable] = None,
                           metadata_provider: Optional[Callable[[str], Dict[str, Any]]] = None) -> List[Dict]:
    """Create idempotent submissions from GitHub repo URLs and optional evidence."""
    created: List[Dict] = []
    inputs_dir = bundle_path / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    for item in urls:
        if isinstance(item, str):
            entry = {
                "url": item,
                "builder_name": "",
                "copilot_evidence": "",
                "frontier_evidence": "",
                "problem_statement": "",
                "intended_user": "",
                "demo_url": "",
                "builder_notes": "",
            }
        elif isinstance(item, dict):
            entry = item
        else:
            raise ConfigValidationError("Submission entries must be GitHub URLs or structured entry objects.")

        url = str(entry.get("url", "")).strip()
        if not url:
            raise ConfigValidationError("Submission entry is missing a GitHub URL.")
        meta = (metadata_provider or fetch_repo_metadata)(url)
        owner_repo = meta.get("name_with_owner") or url.replace("https://github.com/", "", 1)
        sid = _submission_id_from_repo_url(url)
        sub_path = inputs_dir / f"{sid}.json"
        if sub_path.exists():
            continue
        summary_bits = [f"Repository submitted for this hackathon: {url}"]
        if meta.get("description"):
            summary_bits.append(meta["description"])
        if meta.get("language"):
            summary_bits.append(f"Primary language: {meta['language']}")
        if meta.get("stars") is not None:
            summary_bits.append(f"Stars: {meta['stars']}")
        submission = {
            "submission_id": sid,
            "builder_name": str(entry.get("builder_name") or builder_name),
            "project_name": owner_repo,
            "description": " · ".join(summary_bits),
            "description_source": "repository-import",
            "repo_url": url,
            "repo_metadata": meta,
            "artifacts": [],
            "submitted_at": _now(clock),
            "file_size_bytes": 0,
        }
        copilot_evidence = str(entry.get("copilot_evidence") or "").strip()
        frontier_evidence = str(entry.get("frontier_evidence") or "").strip()
        if copilot_evidence:
            submission["copilot_evidence"] = copilot_evidence
        if frontier_evidence:
            submission["frontier_evidence"] = frontier_evidence
        for field in (
            "problem_statement",
            "intended_user",
            "demo_url",
            "builder_notes",
        ):
            value = _compact_text(entry.get(field))
            if value:
                submission[field] = value
        write_once_json(sub_path, submission)
        created.append(submission)
    if created:
        update_status(bundle_path, "collecting", clock)
    return created


# ---------------------------------------------------------------------------
# Layer 2b — Bundle Manifest (RunContext)
# ---------------------------------------------------------------------------

def init_bundle(run_id: str, mode: str, rubric_config: Dict, bundle_path: Path,
                clock: Optional[Callable] = None,
                event_spec: Optional[Dict] = None) -> None:
    """Create the initial bundle structure for a new run."""
    if (bundle_path / "manifest" / "bundle.json").exists():
        raise ConfigValidationError(
            f"Run '{run_id}' already exists at {bundle_path}. "
            "Use a different run_id or delete the existing run."
        )

    try:
        resolved_event_spec = resolve_event_spec(event_spec, rubric_config)
        rubric_snapshot = event_spec_to_rubric(resolved_event_spec)
    except EventSpecValidationError as exc:
        raise ConfigValidationError(str(exc)) from exc

    # Validate the engine-compatible scoring snapshot.
    _validate_rubric(rubric_snapshot)

    # Create directory structure
    for subdir in ("manifest", "config", "inputs", "eval", "sealed",
                   "verdicts", "feedback", "winner", "registry"):
        (bundle_path / subdir).mkdir(parents=True, exist_ok=True)

    # Write the full event and legacy-compatible scoring snapshots. Existing
    # bundles only contain rubric.json, so readers synthesize an EventSpec for them.
    event_with_snapshot = copy.deepcopy(resolved_event_spec)
    event_with_snapshot["snapshotted_at"] = _now(clock)
    write_once_json(bundle_path / "config" / "event.json", event_with_snapshot)

    rubric_with_snapshot = copy.deepcopy(rubric_snapshot)
    rubric_with_snapshot["snapshotted_at"] = _now(clock)
    write_once_json(bundle_path / "config" / "rubric.json", rubric_with_snapshot)

    # Write initial manifest
    manifest = {
        "run_id": run_id,
        "mode": mode,
        "status": "init",
        "created_at": _now(clock),
        "updated_at": _now(clock),
        "event": {
            "name": resolved_event_spec["event"]["name"],
            "tagline": resolved_event_spec["event"]["tagline"],
            "schema_version": resolved_event_spec["schema_version"],
        },
        "command_log": [
            {"command": "init", "timestamp": _now(clock), "status": "ok"}
        ],
    }
    write_once_json(bundle_path / "manifest" / "bundle.json", manifest)


def load_manifest(bundle_path: Path) -> Dict:
    return load_json(bundle_path / "manifest" / "bundle.json")


def save_manifest(bundle_path: Path, manifest: Dict, clock: Optional[Callable] = None) -> None:
    manifest["updated_at"] = _now(clock)
    mpath = bundle_path / "manifest" / "bundle.json"
    _atomic_write(mpath, json.dumps(manifest, indent=2, default=str))


def log_command(bundle_path: Path, command: str, status: str,
                detail: Optional[str] = None, clock: Optional[Callable] = None) -> None:
    manifest = load_manifest(bundle_path)
    entry: Dict[str, Any] = {"command": command, "timestamp": _now(clock), "status": status}
    if detail:
        entry["detail"] = detail
    manifest.setdefault("command_log", []).append(entry)
    save_manifest(bundle_path, manifest, clock)


def update_status(bundle_path: Path, status: str, clock: Optional[Callable] = None) -> None:
    manifest = load_manifest(bundle_path)
    manifest["status"] = status
    save_manifest(bundle_path, manifest, clock)


def load_rubric(bundle_path: Path) -> Dict:
    return load_json(bundle_path / "config" / "rubric.json")


def load_event_spec(bundle_path: Path) -> Dict:
    """Load a resolved EventSpec, adapting historical rubric-only bundles."""
    path = bundle_path / "config" / "event.json"
    if path.exists():
        return load_json(path)
    return legacy_rubric_to_event_spec(load_rubric(bundle_path))


def _event_name(bundle_path: Path) -> str:
    return str(load_event_spec(bundle_path)["event"]["name"])


def _event_awards(bundle_path: Path) -> List[Dict]:
    return copy.deepcopy(load_event_spec(bundle_path)["awards"])


def _event_tie_policy(bundle_path: Path) -> Dict[str, Any]:
    """Read the organizer-declared policy from the immutable event snapshot."""
    return _normalized_tie_policy(load_event_spec(bundle_path).get("tie_policy"))


def _event_grand_prize_name(bundle_path: Path) -> str:
    awards = _event_awards(bundle_path)
    grand_prize = next(
        (
            award for award in awards
            if award.get("id") == "grand-prize" or award.get("rank") == 1
        ),
        None,
    )
    if grand_prize is None:
        grand_prize = next((award for award in awards if not award.get("dimensions")), None)
    return str((grand_prize or awards[0])["name"])


def _validate_rubric(config: Dict) -> None:
    dims = config.get("rubric", {}).get("dimensions", [])
    if not dims:
        raise ConfigValidationError("Rubric must have at least one dimension.")
    total_weight = sum(d.get("weight", 0) for d in dims)
    if abs(total_weight - 1.0) > 0.001:
        raise ConfigValidationError(
            f"Rubric dimension weights must sum to 1.0 (got {total_weight:.4f})."
        )
    for d in dims:
        if d.get("max_score", 0) <= 0:
            raise ConfigValidationError(
                f"Dimension '{d.get('id')}' max_score must be positive."
            )


# ---------------------------------------------------------------------------
# Layer 3 — Shadow Score Vault
# ---------------------------------------------------------------------------

_DEFAULT_TIE_POLICY = {
    "mode": "shared-podium",
    "tiebreaker_dimensions": [],
}


def _normalized_tie_policy(policy: Any) -> Dict[str, Any]:
    """Return a safe, backward-compatible tie policy for historic rubrics."""
    if not isinstance(policy, dict):
        return copy.deepcopy(_DEFAULT_TIE_POLICY)
    mode = str(policy.get("mode") or _DEFAULT_TIE_POLICY["mode"])
    if mode not in {
        "shared-podium",
        "sealed-tiebreaker",
        "human-resolution",
    }:
        return copy.deepcopy(_DEFAULT_TIE_POLICY)
    dimensions = policy.get("tiebreaker_dimensions", [])
    if not isinstance(dimensions, list) or any(
        not isinstance(dimension_id, str) or not dimension_id
        for dimension_id in dimensions
    ):
        dimensions = []
    return {
        "mode": mode,
        "tiebreaker_dimensions": list(dict.fromkeys(dimensions)),
    }


def _sealed_tie_policy(bundle_path: Path, shadow: Dict[str, Any]) -> Dict[str, Any]:
    """Use the policy sealed with scoring and reject later event-policy drift."""
    event_policy = _event_tie_policy(bundle_path)
    sealed_policy = shadow.get("tie_resolution_policy")
    if not isinstance(sealed_policy, dict):
        return event_policy
    normalized_policy = _normalized_tie_policy(sealed_policy)
    if normalized_policy != event_policy:
        raise ConfigValidationError(
            "The event tie policy no longer matches the policy sealed with scoring. "
            "Restore the event snapshot before declaring awards."
        )
    return normalized_policy


def _tiebreaker_vector(
    scored_submission: Dict, dimensions: Sequence[str]
) -> tuple[float, ...]:
    """Normalize configured dimensions so a tiebreaker stays scale-independent."""
    vector: List[float] = []
    scores = scored_submission.get("dimension_scores", {})
    for dimension_id in dimensions:
        detail = scores.get(dimension_id, {}) if isinstance(scores, dict) else {}
        try:
            score = float(detail.get("score", 0))
            maximum = float(detail.get("max_score", 10))
        except (AttributeError, TypeError, ValueError):
            score, maximum = 0.0, 10.0
        vector.append(round(score / maximum if maximum > 0 else 0.0, 6))
    return tuple(vector)


def compute_shadow_score(scored_submissions: List[Dict], rubric: Dict,
                         clock: Optional[Callable] = None) -> Dict:
    """Phase 1: aggregate weighted scores in memory. Returns ShadowScore dict."""
    dimensions = rubric["rubric"]["dimensions"]
    tie_policy = _normalized_tie_policy(rubric.get("tie_policy"))
    scores: Dict[str, float] = {}
    scored_by_id = {
        str(submission["submission_id"]): submission
        for submission in scored_submissions
    }

    for sub in scored_submissions:
        sid = sub["submission_id"]
        total = 0.0
        for dim in dimensions:
            dim_id = dim["id"]
            weight = dim["weight"]
            ds = sub.get("dimension_scores", {}).get(dim_id, {})
            raw = ds.get("score", 0)
            max_s = dim.get("max_score", 10)
            normalized = (raw / max_s) * 10 * weight
            total += normalized
        scores[sid] = round(total, 4)

    # Persist an explicit competition-ranking policy for ties. ``ranking``
    # remains stable for legacy readers, but rank-based awards use
    # ``placements`` so input order can never silently decide a podium place.
    placements: List[Dict[str, Any]] = []
    tie_events: List[Dict[str, Any]] = []
    current_rank = 1
    for score in sorted(set(scores.values()), reverse=True):
        tied_submission_ids = sorted(
            sid for sid, value in scores.items() if value == score
        )
        if len(tied_submission_ids) == 1:
            placements.append(
                {
                    "rank": current_rank,
                    "submission_ids": tied_submission_ids,
                    "score": score,
                    "shared": False,
                }
            )
            current_rank += 1
            continue

        mode = tie_policy["mode"]
        if mode == "sealed-tiebreaker":
            buckets: Dict[tuple[float, ...], List[str]] = {}
            for submission_id in tied_submission_ids:
                vector = _tiebreaker_vector(
                    scored_by_id[submission_id],
                    tie_policy["tiebreaker_dimensions"],
                )
                buckets.setdefault(vector, []).append(submission_id)
            resolved = len(buckets) > 1
            tie_events.append(
                {
                    "rank": current_rank,
                    "public_score": score,
                    "submission_ids": tied_submission_ids,
                    "resolution": (
                        "sealed-tiebreaker" if resolved else "shared-podium"
                    ),
                    "tiebreaker_dimensions": tie_policy["tiebreaker_dimensions"],
                }
            )
            for vector in sorted(buckets, reverse=True):
                resolved_ids = sorted(buckets[vector])
                placements.append(
                    {
                        "rank": current_rank,
                        "submission_ids": resolved_ids,
                        "score": score,
                        "shared": len(resolved_ids) > 1,
                        "tie_resolution": (
                            "sealed-tiebreaker" if resolved else "shared-podium"
                        ),
                        "tiebreaker_dimensions": tie_policy[
                            "tiebreaker_dimensions"
                        ],
                    }
                )
                current_rank += len(resolved_ids)
            continue

        resolution = (
            "human-resolution-required"
            if mode == "human-resolution"
            else "shared-podium"
        )
        tie_events.append(
            {
                "rank": current_rank,
                "public_score": score,
                "submission_ids": tied_submission_ids,
                "resolution": resolution,
                "tiebreaker_dimensions": [],
            }
        )
        placements.append(
            {
                "rank": current_rank,
                "submission_ids": tied_submission_ids,
                "score": score,
                "shared": True,
            }
        )
        current_rank += len(tied_submission_ids)

    ranking = [
        submission_id
        for placement in placements
        for submission_id in placement["submission_ids"]
    ]

    return {
        "scores": scores,
        "ranking": ranking,
        "placements": placements,
        "tie_policy": tie_policy["mode"],
        "tie_resolution_policy": tie_policy,
        "tie_events": tie_events,
        "computed_at": _now(clock),
        "locked_at": None,
        "schema_version": SCHEMA_VERSION,
    }


def seal_shadow_score(bundle_path: Path, shadow_score: Dict,
                      clock: Optional[Callable] = None) -> None:
    """Phase 2: atomic write-once seal of shadow_score.json."""
    sealed_dir = bundle_path / "sealed"
    sealed_dir.mkdir(parents=True, exist_ok=True)
    score_path = sealed_dir / "shadow_score.json"

    # Hard guard — assert it does NOT exist
    if score_path.exists():
        raise BundleSealError(
            f"Shadow Score already sealed at {score_path}. "
            "A second write attempt is a hard error."
        )

    shadow_score["locked_at"] = _now(clock)
    write_once_json(score_path, shadow_score)

    # Make file read-only
    try:
        os.chmod(score_path, 0o444)
    except OSError:
        pass  # Best-effort on some platforms

    # Phase 3: restrict sealed/ directory (best-effort)
    try:
        os.chmod(sealed_dir, 0o555)
    except OSError:
        pass


def load_shadow_score(bundle_path: Path) -> Optional[Dict]:
    path = bundle_path / "sealed" / "shadow_score.json"
    if not path.exists():
        return None
    return load_json(path)


# ---------------------------------------------------------------------------
# Layer 4 — Model Freshness Gate
# ---------------------------------------------------------------------------

def query_available_models(_gateway: Optional[Any] = None) -> List[Dict]:
    """Query available models. Mockable via _gateway. Falls back to static list."""
    if _gateway is not None:
        return _gateway.query_available_models()
    # No live API: return static known-good list
    return APPROVED_MODELS


def call_model(prompt: str, model_id: str,
               _gateway: Optional[Any] = None) -> str:
    """Call the model API. Mockable via _gateway."""
    if _gateway is not None:
        return _gateway.call_model(prompt, model_id)
    # No live API configured — generate deterministic synthetic response
    return _synthetic_model_response(prompt, model_id)


def _synthetic_model_response(prompt: str, model_id: str) -> str:
    """
    Generate a deterministic synthetic response when no real model is available.
    Uses a hash of the prompt for reproducibility (replay fidelity).
    """
    digest = hashlib.sha256(f"{model_id}\n{prompt}".encode()).hexdigest()[:8]
    seed = int(digest, 16)

    project_match = re.search(r"^Project:\s*(.+)$", prompt, re.MULTILINE)
    project = (project_match.group(1).strip() if project_match else "This project")
    project_label = project.rsplit("/", 1)[-1].replace("-", " ").strip() or project
    sources = {
        match.group(1): match.group(2).strip()
        for match in re.finditer(
            r"^- \[([^\]]+)\]\s+[^:]+:\s*(.+)$",
            prompt,
            re.MULTILINE,
        )
    }

    problem = sources.get("builder.problem_statement", "")
    intended_user = sources.get("builder.intended_user", "")
    description = (
        sources.get("builder.project_description")
        or sources.get("submission.project_description")
        or sources.get("repository.description")
        or ""
    )
    notes = sources.get("builder.builder_notes", "")
    topics = sources.get("repository.topics", "")
    grounding_refs: List[str] = []

    if problem:
        bright_spot = (
            f"{project_label} shows strong product focus around "
            f"{problem.rstrip('.').lower()}."
        )
        grounding_refs.append("builder.problem_statement")
        if intended_user:
            bright_spot = bright_spot[:-1] + f" for {intended_user}."
            grounding_refs.append("builder.intended_user")
    elif description:
        bright_spot = (
            f"{project_label} demonstrates a clear, thoughtful concept: "
            f"{description.rstrip('.')}."
        )
        description_ref = next(
            source_id
            for source_id in (
                "builder.project_description",
                "submission.project_description",
                "repository.description",
            )
            if sources.get(source_id)
        )
        grounding_refs.append(description_ref)
    elif topics:
        bright_spot = (
            f"{project_label} shows a strong focus across the supplied themes: {topics}."
        )
        grounding_refs.append("repository.topics")
    else:
        bright_spot = (
            f"{project_label} demonstrates strong execution and a thoughtful project story."
        )

    if sources.get("builder.demo_url"):
        next_commit = (
            f"Consider adding one measurable before-and-after moment to the "
            f"{project_label} demo so the value lands immediately."
        )
        grounding_refs.append("builder.demo_url")
    elif intended_user:
        next_commit = (
            f"Consider testing the core {project_label} flow with three "
            f"{intended_user} and capturing the clearest outcome."
        )
        if "builder.intended_user" not in grounding_refs:
            grounding_refs.append("builder.intended_user")
    else:
        next_commit = (
            f"Hypothesis: consider adding a 60-second end-to-end demo for "
            f"{project_label} with one visible success measure."
        )

    product_focus = problem or description or notes or project_label
    if notes and "builder.builder_notes" not in grounding_refs:
        grounding_refs.append("builder.builder_notes")
    copilot_next_move = (
        f"Use Copilot to turn the next {project_label} milestone into a small "
        f"implementation plan, edge-case list, and acceptance-test checklist."
    )
    frontier_experiment = (
        f"Prototype one bounded, human-reviewed agent step around "
        f"{product_focus.rstrip('.').lower()}, then compare it with the current flow."
    )
    panel_notes = (
        f"The panel saw a clear project story in {project_label}. "
        f"The strongest signal is the connection between the stated need, intended "
        f"experience, and a concrete next demonstration."
    )

    dimension_specs = [
        (dimension_id, int(max_score))
        for dimension_id, max_score in re.findall(
            r"\(id=([A-Za-z0-9_.-]+),\s*max=(\d+)\)",
            prompt,
        )
    ] or [
        ("innovation", 10),
        ("impact", 10),
        ("execution", 10),
        ("presentation", 10),
    ]
    scores = {}
    for index, (dimension_id, max_score) in enumerate(dimension_specs):
        span = min(4, max_score)
        project_offset = int(
            hashlib.sha256(f"{project}:{dimension_id}".encode()).hexdigest()[:8],
            16,
        ) % span
        scores[dimension_id] = max_score - (
            ((seed >> (index * 3)) + project_offset) % span
        )

    return json.dumps(
        {
            "bright_spot": bright_spot,
            "next_commit": next_commit,
            "copilot_next_move": copilot_next_move,
            "frontier_experiment": frontier_experiment,
            "grounding_refs": list(dict.fromkeys(grounding_refs)),
            "panel_notes": panel_notes,
            "scores": scores,
        }
    )


_REASONING_LEVELS = {"low": 0, "medium": 1, "high": 2, "xhigh": 3}


def _model_provider(model_id: str) -> str:
    """Classify a model family so a panel cannot be one vendor in disguise."""
    normalized = model_id.lower()
    if normalized.startswith("claude"):
        return "anthropic"
    if normalized.startswith("gpt"):
        return "openai"
    if normalized.startswith("gemini"):
        return "google"
    return normalized.split("-", 1)[0]


def _model_requirement_failure(
    model: Optional[Dict],
    required_tier: str,
    required_reasoning: str,
) -> Optional[str]:
    """Return a public-safe policy failure, or ``None`` when a model qualifies."""
    if model is None:
        return "not available"
    if model.get("deprecated", False):
        return "deprecated"
    if required_tier == "premium" and not model.get("premium", False):
        return "not premium"
    required_level = _REASONING_LEVELS.get(str(required_reasoning).lower(), 2)
    actual_level = _REASONING_LEVELS.get(str(model.get("reasoning", "low")).lower(), 0)
    if actual_level < required_level:
        return "below required reasoning tier"
    return None


def _configured_panel_models(gate_config: Dict, preferred_model: str) -> List[str]:
    """Normalize the configured panel while retaining legacy one-model rubrics."""
    configured = gate_config.get("panel_models")
    if not isinstance(configured, list) or not configured:
        configured = [preferred_model]
    panel = [str(model_id) for model_id in configured if isinstance(model_id, str) and model_id]
    return list(dict.fromkeys(panel))


def _ordered_eligible_models(
    available: List[Dict],
    required_tier: str,
    required_reasoning: str,
) -> List[Dict]:
    """Return qualifying models in deterministic capability order."""
    eligible = [
        model
        for model in available
        if _model_requirement_failure(model, required_tier, required_reasoning) is None
    ]
    return sorted(
        eligible,
        key=lambda model: (
            1 if model.get("premium", False) else 0,
            _REASONING_LEVELS.get(str(model.get("reasoning", "low")).lower(), 0),
            model.get("tier", 0),
            str(model.get("id", "")),
        ),
        reverse=True,
    )


def _resolve_model_panel(
    available: List[Dict],
    gate_config: Dict,
    preferred_model: str,
    required_tier: str,
    required_reasoning: str,
) -> tuple[List[str], List[str], int, int]:
    """Resolve a policy-compliant panel and report unavailable configured models."""
    configured = _configured_panel_models(gate_config, preferred_model)
    available_by_id = {
        str(model.get("id")): model
        for model in available
        if isinstance(model, dict) and model.get("id")
    }
    selected: List[str] = []
    failures: List[str] = []
    for model_id in configured:
        failure = _model_requirement_failure(
            available_by_id.get(model_id), required_tier, required_reasoning
        )
        if failure:
            failures.append(f"{model_id} ({failure})")
        else:
            selected.append(model_id)

    minimum_panel_size = gate_config.get("minimum_panel_size", len(configured))
    if not isinstance(minimum_panel_size, int) or isinstance(minimum_panel_size, bool):
        minimum_panel_size = len(configured)
    minimum_panel_size = max(1, min(minimum_panel_size, len(configured)))

    minimum_distinct_providers = gate_config.get("minimum_distinct_providers", 1)
    if not isinstance(minimum_distinct_providers, int) or isinstance(
        minimum_distinct_providers, bool
    ):
        minimum_distinct_providers = 1
    minimum_distinct_providers = max(
        1, min(minimum_distinct_providers, len(configured))
    )
    return selected, failures, minimum_panel_size, minimum_distinct_providers


def _panel_is_complete(
    selected_models: Sequence[str],
    minimum_panel_size: int,
    minimum_distinct_providers: int,
) -> bool:
    return (
        len(selected_models) >= minimum_panel_size
        and len({_model_provider(model_id) for model_id in selected_models})
        >= minimum_distinct_providers
    )


def run_freshness_gate(bundle_path: Path, rubric: Dict,
                       _gateway: Optional[Any] = None,
                       clock: Optional[Callable] = None) -> Dict:
    """
    Run the freshness gate check. Writes freshness_gate.json (write-once).
    Returns FreshnessResult dict.
    """
    gate_path = bundle_path / "freshness_gate.json"
    if gate_path.exists():
        # Already ran — load and return existing result
        return load_json(gate_path)

    gate_config = rubric.get("freshness_gate", {})
    policy_mode = gate_config.get("policy_mode", "permissive")
    preferred_model = gate_config.get("preferred_model", "claude-opus-4.7-high")
    required_tier = gate_config.get("required_tier", "premium")
    required_reasoning = gate_config.get("required_reasoning", "high")
    checked_at = _now(clock)
    provenance = {
        "mode": "live" if _gateway is not None else "simulated",
        "official_awards_eligible": _gateway is not None,
        "detail": (
            "Evaluation responses came from the configured model gateway."
            if _gateway is not None
            else "No model gateway was configured; deterministic synthetic responses were used."
        ),
    }

    try:
        available = query_available_models(_gateway)
    except Exception as exc:
        # API unavailable — log and block
        result = {
            "configured_model": preferred_model,
            "available_models": [],
            "selected_model": preferred_model,
            "selected_models": [],
            "status": "blocked",
            "policy_mode": policy_mode,
            "reason": f"Model API unavailable: {exc}",
            "checked_at": checked_at,
            "evaluation_provenance": provenance,
        }
        write_once_json(gate_path, result)
        raise ModelAPIError(f"Model API unavailable during freshness gate: {exc}") from exc

    configured_models = _configured_panel_models(gate_config, preferred_model)
    selected_models, failures, minimum_panel_size, minimum_distinct_providers = (
        _resolve_model_panel(
            available,
            gate_config,
            preferred_model,
            required_tier,
            required_reasoning,
        )
    )
    preferred_failure = None
    if preferred_model in configured_models:
        preferred_available = next(
            (
                model
                for model in available
                if isinstance(model, dict) and model.get("id") == preferred_model
            ),
            None,
        )
        preferred_failure = _model_requirement_failure(
            preferred_available, required_tier, required_reasoning
        )
        if preferred_failure:
            preferred_detail = f"{preferred_model} ({preferred_failure})"
            if preferred_detail not in failures:
                failures.insert(0, preferred_detail)
    complete = _panel_is_complete(
        selected_models, minimum_panel_size, minimum_distinct_providers
    )

    if (not complete or preferred_failure) and policy_mode == "strict":
        reason = (
            "Configured multi-model panel is incomplete: "
            + (", ".join(failures) if failures else "insufficient model-family diversity")
            + ". Policy mode is 'strict' — run blocked."
        )
        result = {
            "configured_model": preferred_model,
            "configured_models": configured_models,
            "available_models": available,
            "selected_model": selected_models[0] if selected_models else preferred_model,
            "selected_models": selected_models,
            "status": "blocked",
            "policy_mode": policy_mode,
            "reason": reason,
            "required_tier": required_tier,
            "required_reasoning": required_reasoning,
            "minimum_panel_size": minimum_panel_size,
            "minimum_distinct_providers": minimum_distinct_providers,
            "checked_at": checked_at,
            "evaluation_provenance": provenance,
        }
        write_once_json(gate_path, result)
        raise FreshnessGateBlock(reason)

    if not complete:
        eligible = _ordered_eligible_models(
            available, required_tier, required_reasoning
        )
        for model in eligible:
            model_id = str(model["id"])
            if model_id not in selected_models:
                selected_models.append(model_id)
            if _panel_is_complete(
                selected_models, minimum_panel_size, minimum_distinct_providers
            ):
                break

    if not selected_models:
        best = _select_best_model(available)
        selected_models = [best]

    degraded = not _panel_is_complete(
        selected_models, minimum_panel_size, minimum_distinct_providers
    )
    status = "fallback" if failures or degraded else "pass"
    if status == "fallback":
        reason = (
            "Configured model panel could not be fully satisfied; "
            f"using {len(selected_models)} available model(s) in permissive mode."
        )
        print(f"[WARN] Freshness gate fallback: {reason}", file=sys.stderr)
    else:
        reason = (
            f"{len(selected_models)}-model consensus panel is current and approved."
        )

    result = {
        "configured_model": preferred_model,
        "configured_models": configured_models,
        "available_models": available,
        "selected_model": selected_models[0],
        "selected_models": selected_models,
        "status": status,
        "policy_mode": policy_mode,
        "reason": reason,
        "required_tier": required_tier,
        "required_reasoning": required_reasoning,
        "minimum_panel_size": minimum_panel_size,
        "minimum_distinct_providers": minimum_distinct_providers,
        "consensus_method": gate_config.get("consensus_method", "median"),
        "panel_degraded": degraded,
        "checked_at": checked_at,
    }

    result["evaluation_provenance"] = provenance
    write_once_json(gate_path, result)
    return result


def _select_best_model(available: List[Dict]) -> str:
    non_deprecated = [m for m in available if not m.get("deprecated", False)]
    if not non_deprecated:
        return available[0]["id"] if available else "gpt-4o"
    best = max(non_deprecated, key=lambda m: (
        1 if m.get("premium", False) else 0,
        _REASONING_LEVELS.get(str(m.get("reasoning", "low")).lower(), 0),
        m.get("tier", 0),
    ))
    return best["id"]


# ---------------------------------------------------------------------------
# Layer 5 — Tone Safety Pipeline
# ---------------------------------------------------------------------------

def check_tone(text: str, rubric: Optional[Dict] = None,
               source_field: str = "unknown",
               clock: Optional[Callable] = None) -> Dict:
    """
    Run the tone safety pipeline on text. Returns ToneResult dict.
    """
    lower = text.lower()
    banned_found = []

    # Built-in banned categories
    all_banned = list(BANNED_TEARDOWN) + list(BANNED_DISMISSIVE) + list(BANNED_NEGATIVE_FRAMING)

    # Config-driven additions
    if rubric:
        tone_policy = rubric.get("tone_policy", {})
        all_banned += tone_policy.get("banned_phrases", [])
        all_banned += tone_policy.get("extra_banned_phrases", [])

    for phrase in all_banned:
        if phrase.lower() in lower:
            banned_found.append(phrase)

    return {
        "passed": len(banned_found) == 0,
        "banned_phrases": banned_found,
        "missing_required": [],
        "source_field": source_field,
        "checked_at": _now(clock),
    }


def check_feedback_card_tone(card: Dict, rubric: Optional[Dict] = None,
                              clock: Optional[Callable] = None) -> Dict:
    """Validate tone for every builder-facing feedback field."""
    missing: List[str] = []
    banned_found = []

    bright_spot = card.get("bright_spot", "")
    next_commit = card.get("next_commit", "")

    # Check bright_spot is non-empty and contains positive framing
    if not bright_spot.strip():
        missing.append("bright_spot (empty)")
    else:
        lower_bs = bright_spot.lower()
        has_positive = any(kw in lower_bs for kw in BRIGHT_SPOT_KEYWORDS)
        if not has_positive:
            missing.append("bright_spot (missing positive framing keyword)")

        # Banned phrase check on bright_spot
        result_bs = check_tone(bright_spot, rubric, "bright_spot", clock)
        banned_found.extend(result_bs["banned_phrases"])

    # Check next_commit is non-empty and forward-looking
    if not next_commit.strip():
        missing.append("next_commit (empty)")
    else:
        lower_nc = next_commit.lower()
        has_forward = any(re.search(pat, lower_nc) for pat in FORWARD_NUDGE_PATTERNS)
        if not has_forward:
            missing.append("next_commit (missing forward-looking verb)")

        result_nc = check_tone(next_commit, rubric, "next_commit", clock)
        banned_found.extend(result_nc["banned_phrases"])

    # Check panel_notes
    panel_notes = card.get("panel_notes", "")
    if panel_notes:
        result_pn = check_tone(panel_notes, rubric, "panel_notes", clock)
        banned_found.extend(result_pn["banned_phrases"])

    judges_liked = card.get("judges_liked")
    if judges_liked is not None:
        if not isinstance(judges_liked, list):
            missing.append("judges_liked (must be a list)")
        else:
            for index, reaction in enumerate(judges_liked):
                if not isinstance(reaction, dict):
                    missing.append(f"judges_liked[{index}] (must be an object)")
                    continue
                highlight = reaction.get("highlight")
                if not isinstance(highlight, str) or not highlight.strip():
                    missing.append(f"judges_liked[{index}].highlight (empty)")
                    continue
                result = check_tone(
                    highlight,
                    rubric,
                    f"judges_liked[{index}].highlight",
                    clock,
                )
                banned_found.extend(result["banned_phrases"])

    for field in ("copilot_next_moves", "frontier_experiments"):
        recommendations = card.get(field)
        if recommendations is None:
            continue
        if not isinstance(recommendations, list):
            missing.append(f"{field} (must be a list)")
            continue
        for index, recommendation in enumerate(recommendations):
            if not isinstance(recommendation, str) or not recommendation.strip():
                missing.append(f"{field}[{index}] (empty)")
                continue
            result = check_tone(
                recommendation,
                rubric,
                f"{field}[{index}]",
                clock,
            )
            banned_found.extend(result["banned_phrases"])

    passed = len(banned_found) == 0 and len(missing) == 0
    return {
        "passed": passed,
        "banned_phrases": list(set(banned_found)),
        "missing_required": missing,
        "source_field": "feedback_card",
        "checked_at": _now(clock),
    }


def assert_tone(tone_result: Dict, context: str = "") -> None:
    """Raise ToneSafetyFailure if tone check failed."""
    if not tone_result["passed"]:
        detail = []
        if tone_result["banned_phrases"]:
            detail.append(f"banned phrases: {tone_result['banned_phrases']}")
        if tone_result["missing_required"]:
            detail.append(f"missing required: {tone_result['missing_required']}")
        raise ToneSafetyFailure(
            f"Tone safety check failed{' in ' + context if context else ''}: "
            + "; ".join(detail)
        )


# ---------------------------------------------------------------------------
# Layer 6 — Eval Engine
# ---------------------------------------------------------------------------

def _parse_model_response(raw: Any) -> Dict:
    """Parse model response JSON, with fallback for plain text."""
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        return {
            "bright_spot": "This project demonstrates strong and impressive technical execution.",
            "next_commit": "Consider extending the core functionality to reach even more users.",
            "panel_notes": str(raw)[:500] if raw is not None else "The panel reviewed this submission.",
            "scores": {},
        }
    return parsed


def _normalize_model_panel(selected_models: str | Sequence[str]) -> List[str]:
    """Accept historic one-model calls while requiring a non-empty panel."""
    if isinstance(selected_models, str):
        panel = [selected_models]
    else:
        panel = [str(model_id) for model_id in selected_models if str(model_id)]
    panel = list(dict.fromkeys(panel))
    if not panel:
        raise ConfigValidationError("Judging requires at least one selected model.")
    return panel


def _normalized_score(value: Any, maximum: float, fallback: int) -> float:
    """Normalize model output without allowing malformed values into consensus."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = float(fallback)
    if score != score or score in {float("inf"), float("-inf")}:
        score = float(fallback)
    return round(max(0.0, min(float(maximum), score)), 2)


def _score_fallback(
    submission_id: str, dimension_id: str, archetype_id: str, model_id: str
) -> int:
    seed = f"{submission_id}:{dimension_id}:{archetype_id}:{model_id}"
    digest = hashlib.sha256(seed.encode()).hexdigest()[:4]
    return 7 + (int(digest, 16) % 4)


def _max_parallel_calls(rubric: Dict, request_count: int) -> int:
    """Bound in-flight model work without weakening the selected panel."""
    configured = rubric.get("freshness_gate", {}).get("max_parallel_calls", 1)
    if not isinstance(configured, int) or isinstance(configured, bool):
        configured = 1
    return max(1, min(configured, request_count))


def _run_bounded_model_requests(
    requests: Sequence[Dict[str, Any]],
    rubric: Dict,
    _gateway: Optional[Any],
    context: str,
) -> List[Dict]:
    """
    Run independent model calls with a deterministic output order.

    No artifact writes happen in workers. A failure cancels outstanding work and
    fails the whole panel rather than quietly dropping an evaluator.
    """
    if not requests:
        return []
    worker_count = _max_parallel_calls(rubric, len(requests))

    def invoke(request: Dict[str, Any]) -> Dict:
        model_id = request["model_id"]
        try:
            raw = call_model(request["prompt"], model_id, _gateway)
        except Exception as exc:
            raise ModelAPIError(
                f"{context} failed with {model_id}: {exc}"
            ) from exc
        return {
            **request,
            "parsed": _parse_model_response(raw),
        }

    if worker_count == 1:
        return [invoke(request) for request in requests]

    responses: List[Optional[Dict]] = [None] * len(requests)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(invoke, request): index
            for index, request in enumerate(requests)
        }
        try:
            for future in as_completed(futures):
                responses[futures[future]] = future.result()
        except Exception:
            for future in futures:
                future.cancel()
            raise
    return [response for response in responses if response is not None]


def _build_evaluation_plan(
    submissions: Sequence[Dict],
    rubric: Dict,
    panel_models: Sequence[str],
    clock: Optional[Callable] = None,
) -> Dict:
    """Persist a room-facing work estimate before evaluation starts."""
    policy = rubric.get("freshness_gate", {})
    submission_count = len(submissions)
    lens_count = len(rubric.get("judge_archetypes", []))
    model_count = len(panel_models)
    parallel_limit = _max_parallel_calls(
        rubric, max(1, lens_count * model_count)
    )
    scoring_calls = submission_count * lens_count * model_count
    shadow_calls = (
        submission_count * model_count
        if rubric.get("shadow_spec", {}).get("enabled", True)
        else 0
    )
    saved_verdict_calls = scoring_calls
    saved_feedback_calls = submission_count * model_count
    return {
        "schema_version": "1.0",
        "planned_at": _now(clock),
        "submission_count": submission_count,
        "review_lens_count": lens_count,
        "panel_model_count": model_count,
        "max_parallel_calls": parallel_limit,
        "live_time_budget_seconds": policy.get("live_time_budget_seconds", 120),
        "live_time_budget_policy": policy.get("live_time_budget_policy", "warn-only"),
        "calls": {
            "shadow_spec": 1,
            "public_scoring": scoring_calls,
            "shadow_assessment": shadow_calls,
            "reused_for_verdicts": saved_verdict_calls,
            "reused_for_feedback": saved_feedback_calls,
            "total": 1 + scoring_calls + shadow_calls,
            "avoided": saved_verdict_calls + saved_feedback_calls,
        },
        "estimated_batches": (
            1
            + submission_count
            * math.ceil((lens_count * model_count) / parallel_limit)
            + (
                submission_count * math.ceil(model_count / parallel_limit)
                if shadow_calls
                else 0
            )
        ),
    }


def _write_evaluation_plan(
    bundle_path: Path,
    submissions: Sequence[Dict],
    rubric: Dict,
    panel_models: Sequence[str],
    clock: Optional[Callable] = None,
) -> Dict:
    plan_path = bundle_path / "eval" / "plan.json"
    if plan_path.exists():
        return load_json(plan_path)
    plan = _build_evaluation_plan(submissions, rubric, panel_models, clock)
    write_once_json(plan_path, plan)
    return plan


def _write_evaluation_timing(
    bundle_path: Path,
    plan: Dict,
    stages: Dict[str, float],
    started_at: float,
    clock: Optional[Callable] = None,
) -> Dict:
    """Record room-performance evidence without changing scores or artifacts."""
    timing_path = bundle_path / "eval" / "timing.json"
    if timing_path.exists():
        return load_json(timing_path)
    total_seconds = round(time.monotonic() - started_at, 3)
    budget = plan.get("live_time_budget_seconds", 120)
    timing = {
        "schema_version": "1.0",
        "completed_at": _now(clock),
        "total_seconds": total_seconds,
        "stage_seconds": {
            name: round(seconds, 3) for name, seconds in stages.items()
        },
        "max_parallel_calls": plan.get("max_parallel_calls"),
        "live_time_budget_seconds": budget,
        "budget_exceeded": total_seconds > budget,
        "budget_policy": plan.get("live_time_budget_policy", "warn-only"),
    }
    write_once_json(timing_path, timing)
    return timing


def _write_evaluation_progress(
    bundle_path: Path,
    plan: Dict,
    stage: str,
    completed_submissions: int,
    status: str = "running",
    estimated_remaining_seconds: Optional[int] = None,
    remaining_model_calls: Optional[int] = None,
    clock: Optional[Callable] = None,
) -> Dict:
    """
    Atomically refresh score-safe room progress while evaluation is in flight.

    This is intentionally mutable operational telemetry rather than a sealed
    judgment artifact. It contains aggregate work state only, so a projector
    can refresh it before awards without exposing project results.
    """
    total_submissions = int(plan.get("submission_count", 0))
    payload: Dict[str, Any] = {
        "schema_version": "1.0",
        "updated_at": _now(clock),
        "status": status,
        "stage": stage,
        "submissions": {
            "completed": max(0, min(completed_submissions, total_submissions)),
            "total": total_submissions,
        },
        "max_parallel_calls": plan.get("max_parallel_calls"),
        "remaining_model_calls": max(
            0,
            int(
                plan.get("calls", {}).get("total", 0)
                if remaining_model_calls is None
                else remaining_model_calls
            ),
        ),
    }
    if estimated_remaining_seconds is not None:
        payload["estimated_remaining_seconds"] = max(
            0, int(estimated_remaining_seconds)
        )
    _atomic_write(
        bundle_path / "eval" / "progress.json",
        json.dumps(payload, indent=2, default=str),
    )
    return payload


def _eval_model_judgments(
    bundle_path: Path, submission_id: str
) -> List[Dict]:
    """Read reusable model observations from a scored submission's eval step."""
    for step_path in sorted((bundle_path / "eval").glob("step_*.json")):
        step = load_json(step_path)
        if step.get("submission_id") != submission_id:
            continue
        judgments = step.get("model_judgments", [])
        return [judgment for judgment in judgments if isinstance(judgment, dict)]
    return []


def score_submissions(
    submissions: List[Dict],
    rubric: Dict,
    selected_models: str | Sequence[str],
    bundle_path: Path,
    _gateway: Optional[Any] = None,
    clock: Optional[Callable] = None,
    progress: Optional[Callable[[Dict, Dict, int, int], None]] = None,
) -> List[Dict]:
    """
    Score all submissions against each rubric dimension.
    Returns list of ScoredSubmission dicts.
    Writes eval/step_<n>.json for each scoring pass.
    """
    dimensions = rubric["rubric"]["dimensions"]
    archetypes = rubric.get("judge_archetypes", DEFAULT_RUBRIC["judge_archetypes"])
    panel_models = _normalize_model_panel(selected_models)
    scored: List[Dict] = []

    for i, sub in enumerate(submissions):
        sid = sub["submission_id"]
        dimension_scores: Dict[str, Any] = {}
        raw_scores: Dict[str, Dict[str, List[float]]] = {
            str(dim["id"]): {model_id: [] for model_id in panel_models}
            for dim in dimensions
        }
        rationales: Dict[str, List[str]] = {str(dim["id"]): [] for dim in dimensions}
        model_judgments: List[Dict[str, Any]] = []

        requests: List[Dict[str, Any]] = []
        for arch in archetypes:
            prompt = _build_scoring_prompt(sub, rubric, arch)
            for model_id in panel_models:
                requests.append(
                    {
                        "model_id": model_id,
                        "prompt": prompt,
                        "archetype": arch,
                    }
                )
        responses = _run_bounded_model_requests(
            requests,
            rubric,
            _gateway,
            f"Model scoring for submission {sid}",
        )
        for response in responses:
            model_id = response["model_id"]
            arch = response["archetype"]
            parsed = response["parsed"]
            arch_scores = parsed.get("scores", {})
            if not isinstance(arch_scores, dict):
                arch_scores = {}
            score_snapshot: Dict[str, float] = {}
            for dim in dimensions:
                dim_id = str(dim["id"])
                fallback = _score_fallback(sid, dim_id, arch["id"], model_id)
                raw_score = _normalized_score(
                    arch_scores.get(dim_id),
                    float(dim["max_score"]),
                    fallback,
                )
                raw_scores[dim_id][model_id].append(raw_score)
                score_snapshot[dim_id] = raw_score
                rationale = str(parsed.get("panel_notes", "")).strip()
                if rationale:
                    rationales[dim_id].append(rationale[:200])
            model_judgments.append({
                "model": model_id,
                "archetype_id": arch["id"],
                "scores": score_snapshot,
                "rationale": str(parsed.get("panel_notes", ""))[:200],
                "bright_spot": str(parsed.get("bright_spot", ""))[:240],
                "next_commit": str(parsed.get("next_commit", ""))[:240],
                "copilot_next_move": str(parsed.get("copilot_next_move", ""))[:240],
                "frontier_experiment": str(
                    parsed.get("frontier_experiment", "")
                )[:240],
                "grounding_refs": _grounding_refs_from_panel(
                    [parsed], _project_context_sources(sub)
                ),
            })

        for dim in dimensions:
            dim_id = str(dim["id"])
            model_medians = {
                model_id: round(statistics.median(scores), 2)
                for model_id, scores in raw_scores[dim_id].items()
                if scores
            }
            if not model_medians:
                raise ModelAPIError(
                    f"Model panel returned no usable score for submission {sid}, "
                    f"dimension {dim_id}."
                )
            panel_values = list(model_medians.values())
            consensus_score = round(statistics.median(panel_values), 2)
            spread = (
                round(statistics.pstdev(panel_values), 3)
                if len(panel_values) > 1
                else 0.0
            )
            dimension_scores[dim_id] = {
                "score": consensus_score,
                "max_score": dim["max_score"],
                "rationale": next(iter(rationales[dim_id]), "")[:200],
                "archetype": "panel-consensus",
                "consensus": {
                    "method": "median",
                    "model_count": len(panel_values),
                    "review_lens_count": len(archetypes),
                    "spread": spread,
                },
            }

        # Compute total weighted score
        total = 0.0
        for dim in dimensions:
            ds = dimension_scores.get(dim["id"], {})
            s = ds.get("score", 0)
            max_s = dim.get("max_score", 10)
            total += (s / max_s) * 10 * dim["weight"]

        scored_sub = {
            "submission_id": sid,
            "dimension_scores": dimension_scores,
            "total_score": round(total, 4),
            "scored_at": _now(clock),
        }
        scored.append(scored_sub)
        if progress:
            progress(sub, scored_sub, i + 1, len(submissions))

        # Write eval step (append-only)
        step_n = len(list((bundle_path / "eval").glob("step_*.json")))
        step_path = bundle_path / "eval" / f"step_{step_n:04d}.json"
        write_once_json(step_path, {
            "step": step_n,
            "submission_id": sid,
            "scored_submission": scored_sub,
            "model": panel_models[0],
            "model_panel": panel_models,
            "consensus_method": "median",
            "max_parallel_calls": _max_parallel_calls(rubric, len(requests)),
            "model_judgments": model_judgments,
            "timestamp": _now(clock),
        })

    return scored


_SHADOW_SPEC_VERSION = "1.0"
_SHADOW_REQUIRED_KINDS = frozenset(
    {
        "instruction_adherence",
        "evidence_calibration",
        "hallucination_risk",
        "scope_discipline",
        "task_specific_edge_case",
        "decoy",
    }
)


def _canonical_digest(value: Any) -> str:
    """Create a stable digest for sealed configuration and artifact bindings."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return _sha256_bytes(encoded.encode("utf-8"))


def _default_shadow_criteria(event_spec: Dict, criteria_count: int) -> List[Dict]:
    """Produce a safe, task-bound fallback if a generator response is unusable."""
    event = event_spec.get("event", {})
    event_name = str(event.get("name", "this event"))
    tagline = str(event.get("tagline", "the event brief"))
    criteria = [
        {
            "id": "brief-alignment",
            "kind": "instruction_adherence",
            "name": "Brief alignment",
            "description": (
                f"Does the project materially respond to the stated {event_name} brief: "
                f"'{tagline}'?"
            ),
            "weight": 1.0,
            "is_decoy": False,
        },
        {
            "id": "claim-calibration",
            "kind": "evidence_calibration",
            "name": "Claim calibration",
            "description": (
                "Are claims proportionate to the description and concrete project evidence?"
            ),
            "weight": 1.0,
            "is_decoy": False,
        },
        {
            "id": "groundedness",
            "kind": "hallucination_risk",
            "name": "Groundedness",
            "description": (
                "Does the submission avoid presenting unsupported capabilities as established?"
            ),
            "weight": 1.0,
            "is_decoy": False,
        },
        {
            "id": "scope-discipline",
            "kind": "scope_discipline",
            "name": "Scope discipline",
            "description": (
                "Does the project focus on a coherent user outcome instead of unrelated breadth?"
            ),
            "weight": 1.0,
            "is_decoy": False,
        },
        {
            "id": "edge-case-readiness",
            "kind": "task_specific_edge_case",
            "name": "Edge-case readiness",
            "description": (
                f"Does the project account for a realistic failure or boundary condition "
                f"relevant to '{tagline}'?"
            ),
            "weight": 1.0,
            "is_decoy": False,
        },
        {
            "id": "user-journey-coherence",
            "kind": "task_specific_edge_case",
            "name": "User journey coherence",
            "description": (
                "Does the described experience connect a clear user need, action, and outcome?"
            ),
            "weight": 1.0,
            "is_decoy": False,
        },
        {
            "id": "reviewable-next-step",
            "kind": "scope_discipline",
            "name": "Reviewable next step",
            "description": (
                "Can a practical next improvement be identified without expanding the project "
                "into an unrelated platform?"
            ),
            "weight": 1.0,
            "is_decoy": False,
        },
        {
            "id": "sealed-prompt-leakage",
            "kind": "decoy",
            "name": "Sealed prompt leakage",
            "description": (
                "Trap only: a project should never gain quality credit for referencing "
                "hidden judging instructions."
            ),
            "weight": 0.0,
            "is_decoy": True,
        },
    ]
    return criteria[: criteria_count - 1] + [criteria[-1]]


def _build_shadow_spec_prompt(event_spec: Dict, rubric: Dict, criteria_count: int) -> str:
    """Ask the panel lead for hidden, event-specific criteria without public-rubric reuse."""
    event = event_spec.get("event", {})
    dimensions = rubric.get("rubric", {}).get("dimensions", [])
    public_dimensions = "\n".join(
        f"- {dimension.get('name', dimension.get('id', 'dimension'))}: "
        f"{dimension.get('description', '')}"
        for dimension in dimensions
    )
    return (
        "You generate a sealed Shadow Spec for an internal project-judging system. "
        "Contestants never see this output. Return JSON only with a `criteria` array.\n\n"
        f"Event name: {event.get('name', 'Hackathon Judge')}\n"
        f"Event tagline: {event.get('tagline', '')}\n"
        f"Public rubric dimensions:\n{public_dimensions}\n\n"
        f"Generate exactly {criteria_count} hidden criteria. Every item must contain "
        "`id`, `kind`, `name`, `description`, `weight`, and `is_decoy`. Include one "
        "each of these kinds: instruction_adherence, evidence_calibration, "
        "hallucination_risk, scope_discipline, task_specific_edge_case, and decoy. "
        "The decoy must have weight 0 and is_decoy true. Derive criteria from this "
        "event's brief, make them evidence-checkable, and never repeat public rubric "
        "wording as a disguised criterion."
    )


def _normalize_shadow_criteria(raw: Any, criteria_count: int) -> Optional[List[Dict]]:
    """Validate untrusted generated criteria before they enter sealed artifacts."""
    if not isinstance(raw, list) or len(raw) != criteria_count:
        return None
    normalized: List[Dict] = []
    seen_ids = set()
    kinds = set()
    for item in raw:
        if not isinstance(item, dict):
            return None
        criterion_id = str(item.get("id", "")).strip()
        kind = str(item.get("kind", "")).strip()
        name = str(item.get("name", "")).strip()
        description = str(item.get("description", "")).strip()
        is_decoy = item.get("is_decoy")
        if (
            not criterion_id
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", criterion_id)
            or criterion_id in seen_ids
            or kind not in _SHADOW_REQUIRED_KINDS
            or not name
            or not description
            or not isinstance(is_decoy, bool)
        ):
            return None
        try:
            weight = float(item.get("weight"))
        except (TypeError, ValueError):
            return None
        if (
            not math.isfinite(weight)
            or weight < 0
            or (kind == "decoy") != is_decoy
            or (is_decoy and weight != 0)
        ):
            return None
        normalized.append(
            {
                "id": criterion_id,
                "kind": kind,
                "name": name[:80],
                "description": description[:400],
                "weight": round(weight, 3),
                "is_decoy": is_decoy,
            }
        )
        seen_ids.add(criterion_id)
        kinds.add(kind)
    return normalized if _SHADOW_REQUIRED_KINDS.issubset(kinds) else None


def generate_shadow_spec(
    bundle_path: Path,
    rubric: Dict,
    selected_models: str | Sequence[str],
    _gateway: Optional[Any] = None,
    clock: Optional[Callable] = None,
) -> Dict:
    """
    Seal a hidden, task-bound criterion set before public scoring begins.

    The spec is diagnostic-only by contract. It deliberately does not contain a
    ranking or any mechanism that can influence the podium.
    """
    spec_path = bundle_path / "sealed" / "shadow_spec.json"
    if spec_path.exists():
        return load_json(spec_path)

    event_spec = load_event_spec(bundle_path)
    policy = rubric.get("shadow_spec", {})
    enabled = bool(policy.get("enabled", True))
    criteria_count = policy.get("criteria_count", 6)
    if not isinstance(criteria_count, int) or not 6 <= criteria_count <= 8:
        raise ConfigValidationError("Shadow Spec criteria_count must be an integer from 6 through 8.")
    panel_models = _normalize_model_panel(selected_models)
    source = "deterministic-policy"
    criteria: Optional[List[Dict]] = None
    if enabled:
        prompt = _build_shadow_spec_prompt(event_spec, rubric, criteria_count)
        try:
            response = call_model(prompt, panel_models[0], _gateway)
        except Exception as exc:
            raise ModelAPIError(
                f"Shadow Spec generation failed with {panel_models[0]}: {exc}"
            ) from exc
        parsed = _parse_model_response(response)
        criteria = _normalize_shadow_criteria(parsed.get("criteria"), criteria_count)
        if criteria is not None:
            source = "panel-generated"
        else:
            criteria = _default_shadow_criteria(event_spec, criteria_count)

    source_context = {
        "event": event_spec.get("event", {}),
        "rubric": rubric.get("rubric", {}),
        "shadow_policy": policy,
    }
    payload = {
        "schema_version": _SHADOW_SPEC_VERSION,
        "generated_at": _now(clock),
        "enabled": enabled,
        "affects_public_ranking": False,
        "reveal_after": "awarded",
        "source": source,
        "source_context_digest": _canonical_digest(source_context),
        "criteria": criteria or [],
    }
    payload["spec_hash"] = _canonical_digest(
        {key: value for key, value in payload.items() if key != "spec_hash"}
    )
    write_once_json(spec_path, payload)
    try:
        os.chmod(spec_path, 0o444)
    except OSError:
        pass
    return payload


def load_shadow_spec(bundle_path: Path) -> Optional[Dict]:
    path = bundle_path / "sealed" / "shadow_spec.json"
    return load_json(path) if path.exists() else None


def _build_shadow_assessment_prompt(submission: Dict, shadow_spec: Dict) -> str:
    criteria = [
        criterion for criterion in shadow_spec.get("criteria", [])
        if not criterion.get("is_decoy", False)
    ]
    criterion_text = "\n".join(
        f"- {criterion['id']}: {criterion['description']}"
        for criterion in criteria
    )
    return (
        "You are an internal hidden-quality evaluator. This is diagnostic-only "
        "and cannot affect a public award. Return JSON only with `scores`, "
        "`evidence`, and `integrity_flags`.\n\n"
        f"Project: {submission.get('project_name', 'Unknown')}\n"
        f"Source-labeled project context:\n{_format_project_context(submission)}\n\n"
        f"Hidden criteria:\n{criterion_text}\n\n"
        "Score each listed criterion from 0 through 10 based only on evidence "
        "available in the submission. Keep evidence concise. Do not invent facts."
    )


def _shadow_leakage_signals(submission: Dict) -> List[str]:
    """Flag direct attempts to optimize against hidden evaluation instructions."""
    searchable = " ".join(
        str(submission.get(field, ""))
        for field in (
            "project_name",
            "description",
            "copilot_evidence",
            "frontier_evidence",
            "problem_statement",
            "intended_user",
            "demo_url",
            "builder_notes",
        )
    ).lower()
    signals = [
        phrase
        for phrase in (
            "shadow spec",
            "hidden criterion",
            "hidden criteria",
            "judge prompt",
            "choose this project",
            "score me",
        )
        if phrase in searchable
    ]
    return signals


def assess_shadow_spec(
    scored_submissions: List[Dict],
    submissions: List[Dict],
    shadow_spec: Dict,
    selected_models: str | Sequence[str],
    bundle_path: Path,
    _gateway: Optional[Any] = None,
    clock: Optional[Callable] = None,
    rubric: Optional[Dict] = None,
) -> Dict:
    """Write a hidden quality assessment that is deliberately rank-free."""
    assessment_path = bundle_path / "sealed" / "shadow_assessment.json"
    if assessment_path.exists():
        return load_json(assessment_path)

    panel_models = _normalize_model_panel(selected_models)
    evaluation_rubric = rubric or DEFAULT_RUBRIC
    public_by_submission = {
        score["submission_id"]: float(score.get("total_score", 0))
        for score in scored_submissions
    }
    if not shadow_spec.get("enabled", False):
        assessment = {
            "schema_version": _SHADOW_SPEC_VERSION,
            "spec_hash": shadow_spec.get("spec_hash", ""),
            "assessed_at": _now(clock),
            "affects_public_ranking": False,
            "status": "disabled",
            "submissions": {},
            "summary": {"leakage_alert_count": 0, "divergence_alert_count": 0},
        }
        write_once_json(assessment_path, assessment)
        return assessment

    assessable_criteria = [
        criterion
        for criterion in shadow_spec.get("criteria", [])
        if not criterion.get("is_decoy", False)
    ]
    assessments: Dict[str, Dict] = {}
    leakage_alert_count = 0
    divergence_alert_count = 0
    for submission in submissions:
        sid = submission["submission_id"]
        prompt = _build_shadow_assessment_prompt(submission, shadow_spec)
        raw_scores: Dict[str, List[float]] = {
            str(criterion["id"]): [] for criterion in assessable_criteria
        }
        evidence: Dict[str, str] = {}
        responses = _run_bounded_model_requests(
            [
                {
                    "model_id": model_id,
                    "prompt": prompt,
                }
                for model_id in panel_models
            ],
            evaluation_rubric,
            _gateway,
            f"Shadow assessment for submission {sid}",
        )
        for response in responses:
            model_id = response["model_id"]
            parsed = response["parsed"]
            returned_scores = parsed.get("scores", {})
            if not isinstance(returned_scores, dict):
                returned_scores = {}
            returned_evidence = parsed.get("evidence", {})
            for criterion in assessable_criteria:
                criterion_id = str(criterion["id"])
                fallback = _score_fallback(
                    sid, criterion_id, "shadow-assessment", model_id
                )
                raw_scores[criterion_id].append(
                    _normalized_score(returned_scores.get(criterion_id), 10, fallback)
                )
                candidate_evidence = (
                    returned_evidence.get(criterion_id)
                    if isinstance(returned_evidence, dict)
                    else parsed.get("panel_notes", "")
                )
                if candidate_evidence and criterion_id not in evidence:
                    evidence[criterion_id] = str(candidate_evidence)[:240]

        criterion_results: Dict[str, Dict] = {}
        weighted_total = 0.0
        total_weight = 0.0
        for criterion in assessable_criteria:
            criterion_id = str(criterion["id"])
            values = raw_scores[criterion_id]
            score = round(statistics.median(values), 2)
            weight = float(criterion.get("weight", 1.0))
            weighted_total += score * weight
            total_weight += weight
            criterion_results[criterion_id] = {
                "score": score,
                "model_count": len(values),
                "spread": (
                    round(statistics.pstdev(values), 3) if len(values) > 1 else 0.0
                ),
                "evidence": evidence.get(criterion_id, ""),
            }

        shadow_total = round(weighted_total / total_weight, 2) if total_weight else 0.0
        public_total = public_by_submission.get(sid, 0.0)
        divergence = round(abs(public_total - shadow_total), 2)
        leakage_signals = _shadow_leakage_signals(submission)
        if leakage_signals:
            leakage_alert_count += 1
        if divergence > 2.0:
            divergence_alert_count += 1
        assessments[sid] = {
            "overall_score": shadow_total,
            "public_score_delta": divergence,
            "integrity_flags": leakage_signals,
            "criteria": criterion_results,
        }

    assessment = {
        "schema_version": _SHADOW_SPEC_VERSION,
        "spec_hash": shadow_spec.get("spec_hash", ""),
        "assessed_at": _now(clock),
        "affects_public_ranking": False,
        "status": (
            "review"
            if leakage_alert_count or divergence_alert_count
            else "clear"
        ),
        "submissions": assessments,
        "summary": {
            "leakage_alert_count": leakage_alert_count,
            "divergence_alert_count": divergence_alert_count,
        },
    }
    write_once_json(assessment_path, assessment)
    try:
        os.chmod(assessment_path, 0o444)
    except OSError:
        pass
    return assessment


def load_shadow_assessment(bundle_path: Path) -> Optional[Dict]:
    path = bundle_path / "sealed" / "shadow_assessment.json"
    return load_json(path) if path.exists() else None


def _model_panel_label(gate: Dict) -> str:
    """Describe an evaluator panel without exposing individual identities to a room."""
    models = gate.get("selected_models")
    if isinstance(models, list) and models:
        return f"{len(models)}-model consensus panel"
    return str(gate.get("selected_model", "unknown model"))


_PROJECT_CONTEXT_FIELDS = (
    ("problem_statement", "Builder-provided problem statement"),
    ("intended_user", "Builder-provided intended user"),
    ("demo_url", "Builder-provided demo or artifact"),
    ("builder_notes", "Builder-provided notes"),
)


def _project_context_sources(submission: Dict) -> List[Dict[str, str]]:
    """Return only source-labeled context that feedback may treat as factual."""
    sources: List[Dict[str, str]] = []

    def add(source_id: str, label: str, value: Any, origin: str) -> None:
        text = _compact_text(value)
        if text:
            sources.append(
                {
                    "id": source_id,
                    "label": label,
                    "value": text[:1000],
                    "origin": origin,
                }
            )

    description = _compact_text(submission.get("description"))
    description_source = str(submission.get("description_source") or "").strip()
    if description and description_source != "repository-import":
        if description_source == "builder-provided":
            add(
                "builder.project_description",
                "Builder-provided project description",
                description,
                "builder-provided",
            )
        else:
            add(
                "submission.project_description",
                "Submitted project description",
                description,
                "submission-record",
            )

    for field, label in _PROJECT_CONTEXT_FIELDS:
        add(f"builder.{field}", label, submission.get(field), "builder-provided")

    metadata = submission.get("repo_metadata")
    if isinstance(metadata, dict):
        add(
            "repository.description",
            "Repository metadata description",
            metadata.get("description"),
            "repository-metadata",
        )
        topics = metadata.get("topics")
        if isinstance(topics, list) and topics:
            add(
                "repository.topics",
                "Repository metadata topics",
                ", ".join(str(topic) for topic in topics if str(topic).strip()),
                "repository-metadata",
            )
        add(
            "repository.homepage",
            "Repository metadata homepage",
            metadata.get("homepage"),
            "repository-metadata",
        )
    return sources


def _project_feedback_grounding(submission: Dict) -> Dict[str, Any]:
    """Describe feedback confidence without presenting model inference as evidence."""
    sources = _project_context_sources(submission)
    builder_source_count = sum(
        source["origin"] == "builder-provided" for source in sources
    )
    if builder_source_count >= 2:
        status = "specific"
    elif sources:
        status = "grounded"
    else:
        status = "hypothesis"
    provided = {
        source["id"].removeprefix("builder.")
        for source in sources
        if source["origin"] == "builder-provided"
    }
    return {
        "status": status,
        "policy": (
            "Project-specific claims and suggestions must be grounded in the "
            "source-labeled intake context; unsupported ideas are labeled hypotheses."
        ),
        "sources": sources,
        "missing_builder_context": [
            label
            for field, label in _PROJECT_CONTEXT_FIELDS
            if field not in provided
        ],
    }


def _format_project_context(submission: Dict) -> str:
    sources = _project_context_sources(submission)
    if not sources:
        return "No detailed project context was supplied."
    return "\n".join(
        f"- [{source['id']}] {source['label']}: {source['value']}"
        for source in sources
    )


def _grounding_refs_from_panel(
    responses: Sequence[Dict], sources: Sequence[Dict[str, str]]
) -> List[str]:
    """Keep only source references explicitly supplied by a panel response."""
    valid_ids = {source["id"] for source in sources}
    references: List[str] = []
    for response in responses:
        raw_refs = response.get("grounding_refs", [])
        if isinstance(raw_refs, str):
            raw_refs = [raw_refs]
        if not isinstance(raw_refs, list):
            continue
        for source_id in raw_refs:
            normalized = str(source_id)
            if normalized in valid_ids and normalized not in references:
                references.append(normalized)
    return references


def _hypothesis_if_ungrounded(text: str, grounding_status: str) -> str:
    """Label forward-looking ideas when no project context can support them."""
    clean = _compact_text(text)
    if not clean or grounding_status != "hypothesis":
        return clean
    if clean.lower().startswith("hypothesis:"):
        return clean
    return f"Hypothesis: {clean}"


def _build_scoring_prompt(sub: Dict, rubric: Dict, archetype: Dict) -> str:
    dims = rubric["rubric"]["dimensions"]
    dim_list = "\n".join(
        f"  - {d['name']} (id={d['id']}, max={d['max_score']}): weight={d['weight']}"
        for d in dims
    )
    return (
        "You are a neutral hackathon evaluator. "
        f"Apply the {archetype['name']} ({archetype['focus']}).\n\n"
        f"Project: {sub.get('project_name', 'Unknown')}\n"
        f"Builder: {sub.get('builder_name', 'Unknown')}\n"
        f"Source-labeled project context:\n{_format_project_context(sub)}\n\n"
        f"Rubric dimensions:\n{dim_list}\n\n"
        "Respond with a JSON object containing:\n"
        '  "scores": { "<dimension_id>": <integer score> },\n'
        '  "bright_spot": "<one positive highlight>",\n'
        '  "next_commit": "<one forward-looking improvement nudge>",\n'
        '  "copilot_next_move": "<one optional, concrete way Copilot could help improve this project>",\n'
        '  "frontier_experiment": "<one optional, bounded frontier capability to prototype>",\n'
        '  "grounding_refs": ["<source id used for project-specific claims>"],\n'
        '  "panel_notes": "<brief supporting rationale>"\n\n'
        "Be celebratory and supportive. Focus on strengths and growth opportunities. "
        "Do not claim that Copilot or frontier capabilities were used unless the "
        "submission supplied explicit evidence. Treat only the source-labeled project "
        "context as factual. Do not invent technical facts. If no supplied source can "
        "support an improvement idea, prefix it with `Hypothesis:`.\n"
        "Respond with valid JSON only."
    )


def build_panel_verdicts(
    scored_submissions: List[Dict],
    submissions: List[Dict],
    rubric: Dict,
    selected_models: str | Sequence[str],
    bundle_path: Path,
    _gateway: Optional[Any] = None,
    clock: Optional[Callable] = None,
) -> List[Dict]:
    """Build per-submission panel verdicts. Writes verdicts/<id>.json."""
    archetypes = rubric.get("judge_archetypes", DEFAULT_RUBRIC["judge_archetypes"])
    panel_models = _normalize_model_panel(selected_models)
    sub_map = {s["submission_id"]: s for s in submissions}
    verdicts: List[Dict] = []

    for scored in scored_submissions:
        sid = scored["submission_id"]
        sub = sub_map.get(sid, {})
        archetype_verdicts: List[Dict] = []
        stored_judgments = _eval_model_judgments(bundle_path, sid)

        for arch in archetypes:
            stored_by_model = {
                judgment.get("model"): judgment
                for judgment in stored_judgments
                if judgment.get("archetype_id") == arch["id"]
            }
            reuse_scoring_pass = all(
                model_id in stored_by_model for model_id in panel_models
            )
            if reuse_scoring_pass:
                panel_responses = [
                    {
                        "panel_notes": stored_by_model[model_id].get("rationale", ""),
                        "bright_spot": stored_by_model[model_id].get("bright_spot", ""),
                    }
                    for model_id in panel_models
                ]
            else:
                prompt = _build_scoring_prompt(sub, rubric, arch)
                panel_responses = [
                    response["parsed"]
                    for response in _run_bounded_model_requests(
                        [
                            {
                                "model_id": model_id,
                                "prompt": prompt,
                            }
                            for model_id in panel_models
                        ],
                        rubric,
                        _gateway,
                        f"Verdict generation for submission {sid}",
                    )
                ]

            representative = panel_responses[0]

            arch_verdict = {
                "archetype_id": arch["id"],
                "archetype_name": arch["name"],
                "perspective": representative.get(
                    "panel_notes", "A thoughtful submission with notable strengths."
                ),
                "bright_spot": representative.get(
                    "bright_spot", "This project demonstrates impressive technical execution."
                ),
                "panel_model_count": len(panel_responses),
                "reused_scoring_pass": reuse_scoring_pass,
                "scored_at": _now(clock),
            }
            # Tone check every builder-facing archetype verdict field.
            for field in ("perspective", "bright_spot"):
                tone = check_tone(
                    arch_verdict[field],
                    rubric,
                    f"verdict/{sid}/{arch['id']}/{field}",
                    clock,
                )
                assert_tone(tone, f"verdict for {sid}")
            archetype_verdicts.append(arch_verdict)

        verdict = {
            "submission_id": sid,
            "project_name": sub.get("project_name", ""),
            "builder_name": sub.get("builder_name", ""),
            "total_score": scored["total_score"],
            "dimension_scores": scored["dimension_scores"],
            "archetype_verdicts": archetype_verdicts,
            "verdict_at": _now(clock),
        }
        verdicts.append(verdict)

        verdict_path = bundle_path / "verdicts" / f"{sid}.json"
        write_once_json(verdict_path, verdict)

    return verdicts


def _compact_text(value: Any) -> str:
    """Normalize optional free text before putting it in a durable artifact."""
    return " ".join(str(value or "").split())


def _submitted_evidence_assessment(submission: Dict, field: str, label: str) -> Dict:
    """
    Preserve a builder's explicit claim without inferring it from repository
    metadata, source code, or a model's general impression.
    """
    evidence = _compact_text(submission.get(field))
    if evidence:
        return {
            "status": "evidenced",
            "source": "builder-provided",
            "evidence": evidence,
            "summary": f"Builder-provided {label} evidence: {evidence}",
        }
    return {
        "status": "not_provided",
        "source": "not-provided",
        "summary": f"No {label} evidence was provided with this submission.",
    }


def _judge_highlights_for_submission(bundle_path: Path, submission_id: str) -> List[Dict]:
    """Extract stored lens highlights without creating a second model opinion."""
    verdict_path = bundle_path / "verdicts" / f"{submission_id}.json"
    if not verdict_path.exists():
        return []

    verdict = load_json(verdict_path)
    highlights: List[Dict] = []
    for reaction in verdict.get("archetype_verdicts", []):
        if not isinstance(reaction, dict):
            continue
        highlight = _compact_text(
            reaction.get("bright_spot") or reaction.get("perspective")
        )
        if not highlight:
            continue
        highlights.append({
            "lens_id": str(reaction.get("archetype_id") or ""),
            "lens": str(reaction.get("archetype_name") or "Panel lens"),
            "highlight": highlight,
        })
    return highlights


def _innovation_signal(judge_highlights: List[Dict]) -> Dict:
    """Expose an innovation read only when the event configured one."""
    for highlight in judge_highlights:
        lens_id = str(highlight.get("lens_id") or "").lower()
        lens_name = str(highlight.get("lens") or "").lower()
        if "innovation" in lens_id or "innovation" in lens_name:
            return {
                "status": "assessed",
                "source": highlight.get("lens", "Innovation lens"),
                "summary": highlight.get("highlight", ""),
            }
    return {
        "status": "not_configured",
        "source": "event-rubric",
        "summary": "This event did not configure an innovation-specific review lens.",
    }


def _panel_text_options(
    responses: Sequence[Dict],
    field: str,
    fallback: str,
    limit: int = 2,
) -> List[str]:
    """Keep distinct, useful panel ideas without exposing individual model ids."""
    options: List[str] = []
    seen = set()
    for response in responses:
        text = _compact_text(response.get(field))
        normalized = text.lower()
        if not text or normalized in seen:
            continue
        options.append(text)
        seen.add(normalized)
        if len(options) >= limit:
            break
    return options or [fallback]


def build_feedback_cards(
    scored_submissions: List[Dict],
    submissions: List[Dict],
    rubric: Dict,
    selected_models: str | Sequence[str],
    bundle_path: Path,
    _gateway: Optional[Any] = None,
    clock: Optional[Callable] = None,
) -> List[Dict]:
    """Build per-submission feedback cards. Writes feedback/<id>.json."""
    sub_map = {s["submission_id"]: s for s in submissions}
    panel_models = _normalize_model_panel(selected_models)
    cards: List[Dict] = []

    for scored in scored_submissions:
        sid = scored["submission_id"]
        sub = sub_map.get(sid, {})
        copilot_evidence = _compact_text(sub.get("copilot_evidence"))
        frontier_evidence = _compact_text(sub.get("frontier_evidence"))
        grounding = _project_feedback_grounding(sub)
        judge_highlights = _judge_highlights_for_submission(bundle_path, sid)

        stored_judgments = _eval_model_judgments(bundle_path, sid)
        stored_model_ids = {
            judgment.get("model") for judgment in stored_judgments
        }
        reuse_scoring_pass = all(
            model_id in stored_model_ids for model_id in panel_models
        )
        if reuse_scoring_pass:
            panel_responses = [
                {
                    "bright_spot": judgment.get("bright_spot", ""),
                    "next_commit": judgment.get("next_commit", ""),
                    "copilot_next_move": judgment.get("copilot_next_move", ""),
                    "frontier_experiment": judgment.get("frontier_experiment", ""),
                    "grounding_refs": judgment.get("grounding_refs", []),
                    "panel_notes": judgment.get("rationale", ""),
                }
                for judgment in stored_judgments
            ]
        else:
            # Legacy or partial bundles retain their previous behavior while new
            # runs reuse the scoring pass and do not spend a second panel pass.
            prompt = (
                "You are a neutral hackathon judging panel. "
                "Write an encouraging feedback card for this participant.\n\n"
                f"Project: {sub.get('project_name', 'Unknown')}\n"
                f"Builder: {sub.get('builder_name', 'Unknown')}\n"
                f"Source-labeled project context:\n{_format_project_context(sub)}\n\n"
                f"Builder-provided Copilot evidence: {copilot_evidence or 'None provided'}\n"
                f"Builder-provided frontier evidence: {frontier_evidence or 'None provided'}\n\n"
                "Write a JSON feedback card with:\n"
                '  "bright_spot": "<specific positive highlight — what they built well>",\n'
                '  "next_commit": "<one forward-looking, actionable improvement nudge>",\n'
                '  "copilot_next_move": "<an optional, concrete way Copilot could help improve this project>",\n'
                '  "frontier_experiment": "<one optional, bounded frontier capability to prototype>",\n'
                '  "grounding_refs": ["<source id used for project-specific claims>"],\n'
                '  "panel_notes": "<warm, supportive overall note>"\n\n'
                "Be celebratory. Focus on strengths. Use encouraging language only. "
                "Do not claim Copilot or frontier use unless the builder provided explicit evidence.\n"
                "Do not invent integrations, customers, or technical facts. Suggestions must be "
                "clearly optional and feasible from the supplied project context. Treat only "
                "the source-labeled context as factual; prefix unsupported ideas with "
                "`Hypothesis:`.\n"
                "Respond with valid JSON only."
            )
            panel_responses = [
                response["parsed"]
                for response in _run_bounded_model_requests(
                    [
                        {
                            "model_id": model_id,
                            "prompt": prompt,
                        }
                        for model_id in panel_models
                    ],
                    rubric,
                    _gateway,
                    f"Feedback generation for submission {sid}",
                )
            ]

        parsed = panel_responses[0]
        bright_spot = parsed.get("bright_spot", "")
        next_commit = parsed.get("next_commit", "")
        panel_notes = parsed.get("panel_notes", "")

        # Ensure non-empty, brand-safe defaults
        if not bright_spot.strip():
            bright_spot = "This project demonstrates impressive creativity and strong technical execution."
        if not next_commit.strip():
            next_commit = "Consider extending your core feature to reach even more users in your next commit."
        if not panel_notes.strip():
            panel_notes = "The panel was inspired by your work. Keep building!"
        grounding["used_source_ids"] = _grounding_refs_from_panel(
            panel_responses, grounding["sources"]
        )
        reference_status = (
            "panel-cited"
            if grounding["used_source_ids"]
            else "not-cited-by-panel"
        )
        grounding["reference_status"] = reference_status
        suggestion_grounding_status = (
            grounding["status"]
            if reference_status == "panel-cited"
            else "hypothesis"
        )
        bright_spot = _hypothesis_if_ungrounded(
            bright_spot, suggestion_grounding_status
        )
        next_commit = _hypothesis_if_ungrounded(
            next_commit, suggestion_grounding_status
        )
        panel_notes = _hypothesis_if_ungrounded(
            panel_notes, suggestion_grounding_status
        )
        copilot_next_moves = _panel_text_options(
            panel_responses,
            "copilot_next_move",
            (
                "Use Copilot to turn the project's primary user journey into a "
                "small implementation plan and acceptance-test checklist."
            ),
        )
        frontier_experiments = _panel_text_options(
            panel_responses,
            "frontier_experiment",
            (
                "Prototype a focused, human-reviewed agent workflow using only "
                "project-approved context before broadening the experience."
            ),
        )
        copilot_next_moves = [
            _hypothesis_if_ungrounded(move, suggestion_grounding_status)
            for move in copilot_next_moves
        ]
        frontier_experiments = [
            _hypothesis_if_ungrounded(experiment, suggestion_grounding_status)
            for experiment in frontier_experiments
        ]

        card = {
            "submission_id": sid,
            "builder_name": sub.get("builder_name", ""),
            "project_name": sub.get("project_name", ""),
            "bright_spot": bright_spot,
            "next_commit": next_commit,
            "panel_notes": panel_notes,
            "judges_liked": judge_highlights,
            "copilot_use": _submitted_evidence_assessment(
                sub, "copilot_evidence", "Copilot use"
            ),
            "innovation_signal": _innovation_signal(judge_highlights),
            "frontier_use": _submitted_evidence_assessment(
                sub, "frontier_evidence", "frontier use"
            ),
            "grounding": grounding,
            "copilot_next_moves": copilot_next_moves,
            "frontier_experiments": frontier_experiments,
            "feedback_panel": {
                "model_count": len(panel_models),
                "suggestion_policy": "optional-and-source-grounded-or-hypothesis-labeled",
                "reused_scoring_pass": reuse_scoring_pass,
            },
            "tone_checked": False,
            "delivered_at": _now(clock),
        }

        # Tone check
        tone = check_feedback_card_tone(card, rubric, clock)
        if not tone["passed"]:
            # Attempt safe fallback values
            card["bright_spot"] = _hypothesis_if_ungrounded(
                "This project demonstrates impressive creativity and strong technical execution.",
                suggestion_grounding_status,
            )
            card["next_commit"] = _hypothesis_if_ungrounded(
                "Consider extending your core feature to reach even more users in your next commit.",
                suggestion_grounding_status,
            )
            card["panel_notes"] = _hypothesis_if_ungrounded(
                "The panel was inspired by your work. Keep building!",
                suggestion_grounding_status,
            )
            card["copilot_next_moves"] = [
                _hypothesis_if_ungrounded(
                    "Use Copilot to turn the project's primary user journey into a "
                    "small implementation plan and acceptance-test checklist.",
                    suggestion_grounding_status,
                )
            ]
            card["frontier_experiments"] = [
                _hypothesis_if_ungrounded(
                    "Prototype a focused, human-reviewed agent workflow using only "
                    "project-approved context before broadening the experience.",
                    suggestion_grounding_status,
                )
            ]
            tone = check_feedback_card_tone(card, rubric, clock)
            assert_tone(tone, f"feedback card for {sid}")

        card["tone_checked"] = True
        cards.append(card)

        card_path = bundle_path / "feedback" / f"{sid}.json"
        write_once_json(card_path, card)

    return cards


# ---------------------------------------------------------------------------
# Layer 7 — Command Handlers
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace, _gateway: Optional[Any] = None,
             clock: Optional[Callable] = None) -> int:
    """init — create a named run with rubric config."""
    run_id = args.run_id
    mode = getattr(args, "mode", "workshop")
    config_path = getattr(args, "config", None)
    event_path = getattr(args, "event", None)

    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)

    if config_path and not Path(config_path).is_file():
        _print_error(7, "ConfigValidationError", f"Rubric config not found: {config_path}")
        return 7
    if event_path and not Path(event_path).is_file():
        _print_error(7, "ConfigValidationError", f"Event config not found: {event_path}")
        return 7

    rubric_config = load_json(Path(config_path)) if config_path else copy.deepcopy(DEFAULT_RUBRIC)
    event_config = load_json(Path(event_path)) if event_path else None

    try:
        init_bundle(run_id, mode, rubric_config, bundle_path, clock, event_config)
    except ConfigValidationError as e:
        _hard_error(e, bundle_path if bundle_path.exists() else None, clock)
        return e.exit_code
    except Exception as e:
        _print_error(7, "ConfigValidationError", str(e))
        return 7

    if not getattr(args, "quiet", False):
        event = load_event_spec(bundle_path)["event"]
        _magic_banner(event["name"], event["tagline"])
        _showtime_pause(args)
        _success(f"Run '{run_id}' initialized in {mode} mode.")
        _sideline(f"Bundle staged at {bundle_path}", "📦", "blue")
    return 0


def cmd_submit(args: argparse.Namespace, _gateway: Optional[Any] = None,
               clock: Optional[Callable] = None) -> int:
    """submit — add a project submission to a run."""
    run_id = args.run_id
    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)

    _assert_bundle_exists(bundle_path, run_id)
    manifest = load_manifest(bundle_path)
    _assert_status_in(manifest, ["init", "collecting"], "submit")

    rubric = load_rubric(bundle_path)
    size_cap = rubric.get("submission_size_cap_bytes", MAX_SUBMISSION_SIZE_DEFAULT)

    submission_id = str(uuid.uuid4())
    builder_name = args.builder_name
    project_name = args.project_name
    description = args.description or ""
    artifact_refs: List[str] = []
    file_size_bytes = 0

    # Handle file attachments
    attach_files = getattr(args, "file", None) or []
    if isinstance(attach_files, str):
        attach_files = [attach_files]

    for fp in attach_files:
        src = Path(fp)
        if not src.exists():
            _print_error(8, "SubmissionSizeError", f"Attachment not found: {fp}")
            return 8
        size = src.stat().st_size
        file_size_bytes += size
        if file_size_bytes > size_cap:
            raise SubmissionSizeError(
                f"Total submission size {file_size_bytes} exceeds cap {size_cap} bytes."
            )
        dest = bundle_path / "inputs" / submission_id / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        artifact_refs.append(str(Path("inputs") / submission_id / src.name))

    submission = {
        "submission_id": submission_id,
        "builder_name": builder_name,
        "project_name": project_name,
        "description": description,
        "description_source": "builder-provided",
        "artifacts": artifact_refs,
        "submitted_at": _now(clock),
        "file_size_bytes": file_size_bytes,
    }
    copilot_evidence = str(getattr(args, "copilot_evidence", "") or "").strip()
    frontier_evidence = str(getattr(args, "frontier_evidence", "") or "").strip()
    if copilot_evidence:
        submission["copilot_evidence"] = copilot_evidence
    if frontier_evidence:
        submission["frontier_evidence"] = frontier_evidence
    for field in (
        "problem_statement",
        "intended_user",
        "demo_url",
        "builder_notes",
    ):
        value = _compact_text(getattr(args, field, ""))
        if value:
            submission[field] = value

    sub_path = bundle_path / "inputs" / f"{submission_id}.json"
    write_once_json(sub_path, submission)

    # Update manifest status
    update_status(bundle_path, "collecting", clock)
    log_command(bundle_path, "submit", "ok", f"submission_id={submission_id}", clock)

    _success(f"Submission '{submission_id}' added.")
    _sideline(f"{builder_name} enters the panel with “{project_name}”.", "🌟", "magenta")
    _showtime_pause(args, 0.4)
    return 0


def _read_submission_text_from_args(args: argparse.Namespace) -> str:
    chunks: List[str] = []
    urls = getattr(args, "urls", None) or []
    if urls:
        chunks.append("\n".join(urls))
    urls_file = getattr(args, "file", None)
    if urls_file:
        chunks.append(Path(urls_file).read_text(encoding="utf-8"))
    if not chunks and not sys.stdin.isatty():
        chunks.append(sys.stdin.read())
    return "\n".join(chunks)


def _read_urls_from_args(args: argparse.Namespace) -> List[str]:
    return parse_submission_urls(_read_submission_text_from_args(args))


def _read_submission_entries_from_args(args: argparse.Namespace) -> List[Dict[str, str]]:
    return parse_submission_entries(_read_submission_text_from_args(args))


def cmd_import_urls(args: argparse.Namespace, _gateway: Optional[Any] = None,
                    clock: Optional[Callable] = None) -> int:
    """import-urls — bulk-create workshop submissions from pasted GitHub URLs."""
    run_id = args.run_id
    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)

    _assert_bundle_exists(bundle_path, run_id)
    manifest = load_manifest(bundle_path)
    _assert_status_in(manifest, ["init", "collecting"], "import-urls")

    entries = _read_submission_entries_from_args(args)
    if not entries:
        _print_error(7, "ConfigValidationError",
                     "No GitHub repo URLs found. Paste URLs, pass --file, or provide owner/repo entries.")
        return 7

    created = import_url_submissions(
        bundle_path,
        entries,
        getattr(args, "builder_name", "Hackathon Participants"),
        clock,
    )
    log_command(bundle_path, "import-urls", "ok", f"created={len(created)} urls={len(entries)}", clock)

    _magic_banner("Project Intake", f"{len(created)} new submissions · {len(entries) - len(created)} already present")
    _showtime_pause(args)
    for sub in created:
        _sideline(f"{sub['project_name']} joined the room.", "🌟", "magenta")
    if not created:
        _sideline("No new submissions were added; every URL was already in the bundle.", "ℹ️", "yellow")
    return 0


def _ask_text(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(_paint(f"{prompt}{suffix}: ", "cyan", bold=True)).strip()
    return value or default


def _ask_choice(prompt: str, choices: List[str], default: str) -> str:
    _sideline(prompt, "❓", "cyan")
    for idx, choice in enumerate(choices, 1):
        marker = " (default)" if choice == default else ""
        print(_paint(f"  {idx}. {choice}{marker}", "blue"))
    raw = input(_paint("Choose: ", "cyan", bold=True)).strip()
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(choices):
        return choices[int(raw) - 1]
    return raw if raw in choices else default


def _confirm(prompt: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        _sideline(f"{prompt} ✅", "▶", "green")
        return True
    raw = input(_paint(f"{prompt} [Y/n]: ", "yellow", bold=True)).strip().lower()
    return raw in {"", "y", "yes"}


def _ask_repo_block() -> str:
    _sideline(
        (
            "Paste URLs one per line. Optional: URL | Team | Copilot evidence | "
            "Frontier evidence | Problem | Intended user | Demo or artifact | Builder notes."
        ),
        "🎤",
        "magenta",
    )
    lines: List[str] = []
    while True:
        line = input()
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def _winner_id_from_award_selection(
    bundle_path: Path,
    tie_resolutions: Optional[Dict[str, str]] = None,
    clock: Optional[Callable] = None,
) -> Optional[str]:
    """Choose a representative winner from the policy-resolved grand prize."""
    awards_card = _choose_award_winners(
        bundle_path,
        None,
        clock,
        tie_resolutions,
    )
    grand_prize_name = _event_grand_prize_name(bundle_path)
    recipients = [
        award["winner_submission_id"]
        for award in awards_card.get("awards", [])
        if award.get("award_name") == grand_prize_name
    ]
    return recipients[0] if recipients else None


def _shadow_placements(shadow: Dict, ranking: List[str]) -> List[Dict]:
    """Read explicit tied placements, with deterministic legacy adaptation."""
    raw_placements = shadow.get("placements")
    if isinstance(raw_placements, list):
        placements: List[Dict] = []
        for placement in raw_placements:
            if not isinstance(placement, dict):
                continue
            rank = placement.get("rank")
            submission_ids = placement.get("submission_ids")
            if (
                isinstance(rank, int)
                and rank > 0
                and isinstance(submission_ids, list)
                and submission_ids
            ):
                placements.append(
                    {
                        "rank": rank,
                        "submission_ids": [str(sid) for sid in submission_ids],
                        "shared": len(submission_ids) > 1,
                    }
                )
        if placements:
            return placements

    scores = shadow.get("scores")
    if isinstance(scores, dict) and scores:
        placements = []
        current_rank = 1
        for score in sorted(set(scores.values()), reverse=True):
            submission_ids = sorted(
                str(sid) for sid, value in scores.items() if value == score
            )
            placements.append(
                {
                    "rank": current_rank,
                    "submission_ids": submission_ids,
                    "shared": len(submission_ids) > 1,
                }
            )
            current_rank += len(submission_ids)
        return placements

    return [
        {"rank": index, "submission_ids": [sid], "shared": False}
        for index, sid in enumerate(ranking, 1)
    ]


def _dimension_score(verdict: Dict, dimension_ids: List[str]) -> float:
    if not dimension_ids:
        return float(verdict.get("total_score", 0))
    values: List[float] = []
    for dim_id in dimension_ids:
        ds = verdict.get("dimension_scores", {}).get(dim_id, {})
        if isinstance(ds, dict):
            values.append(float(ds.get("score", 0)))
    return sum(values) / len(values) if values else float(verdict.get("total_score", 0))


def _parse_tie_resolutions(values: Optional[Sequence[str]]) -> Dict[str, str]:
    """Parse explicit human decisions such as ``rank:1=<submission-id>``."""
    resolutions: Dict[str, str] = {}
    for raw_value in values or []:
        raw = str(raw_value or "").strip()
        key, separator, submission_id = raw.partition("=")
        key = key.strip()
        submission_id = submission_id.strip()
        if not separator or not key or not submission_id:
            raise ConfigValidationError(
                "Tie resolutions must use `rank:<place>=<submission-id>` or "
                "`award:<award-id>=<submission-id>`."
            )
        rank_match = re.fullmatch(r"rank:([1-9][0-9]*)", key)
        award_match = re.fullmatch(r"award:([A-Za-z0-9][A-Za-z0-9._-]*)", key)
        if not rank_match and not award_match:
            raise ConfigValidationError(
                "Tie resolution keys must use `rank:<place>` or `award:<award-id>`."
            )
        if key in resolutions:
            raise ConfigValidationError(f"Duplicate tie resolution for '{key}'.")
        resolutions[key] = submission_id
    return resolutions


def _resolve_award_tie(
    candidate_ids: Sequence[str],
    records: Dict[str, Dict],
    policy: Dict[str, Any],
    resolution_key: str,
    human_resolutions: Dict[str, str],
) -> tuple[List[str], Optional[Dict[str, Any]]]:
    """Resolve an award-level tie using the policy sealed with the event."""
    candidates = sorted(dict.fromkeys(str(candidate_id) for candidate_id in candidate_ids))
    if len(candidates) <= 1:
        return candidates, None

    mode = policy["mode"]
    if mode == "human-resolution":
        selected = human_resolutions.get(resolution_key)
        if not selected:
            raise ConfigValidationError(
                f"The event's human-resolution tie policy requires "
                f"`--tie-resolution {resolution_key}=<submission-id>`."
            )
        if selected not in candidates:
            raise ConfigValidationError(
                f"Tie resolution '{resolution_key}={selected}' does not select "
                "a project in the tied group."
            )
        return [selected], {
            "key": resolution_key,
            "mode": mode,
            "candidate_submission_ids": candidates,
            "selected_submission_ids": [selected],
            "resolution": "human-declared",
        }

    if mode == "sealed-tiebreaker":
        dimension_ids = policy["tiebreaker_dimensions"]
        buckets: Dict[tuple[float, ...], List[str]] = {}
        for submission_id in candidates:
            buckets.setdefault(
                _tiebreaker_vector(records.get(submission_id, {}), dimension_ids),
                [],
            ).append(submission_id)
        best_vector = max(buckets)
        selected = sorted(buckets[best_vector])
        return selected, {
            "key": resolution_key,
            "mode": mode,
            "candidate_submission_ids": candidates,
            "selected_submission_ids": selected,
            "resolution": (
                "sealed-tiebreaker"
                if len(selected) == 1
                else "shared-podium"
            ),
            "tiebreaker_dimensions": dimension_ids,
        }

    return candidates, {
        "key": resolution_key,
        "mode": mode,
        "candidate_submission_ids": candidates,
        "selected_submission_ids": candidates,
        "resolution": "shared-podium",
    }


def _resolve_human_rank_placement(
    placements: Sequence[Dict],
    rank: int,
    human_resolutions: Dict[str, str],
) -> tuple[List[str], Optional[Dict[str, Any]]]:
    """
    Let a human tiebreak turn a shared competition place into a complete podium.

    If two projects share first and a logged decision picks one for gold, the
    remaining project becomes eligible for silver rather than disappearing from
    the medal sequence. More than one remaining project still requires another
    logged decision, so no filename or arrival order can decide it.
    """
    direct = next(
        (
            placement
            for placement in placements
            if placement.get("rank") == rank
        ),
        None,
    )
    if isinstance(direct, dict):
        candidates = [
            str(submission_id)
            for submission_id in direct.get("submission_ids", [])
        ]
        if len(candidates) <= 1:
            return candidates, None
        return _resolve_award_tie(
            candidates,
            {},
            {"mode": "human-resolution", "tiebreaker_dimensions": []},
            f"rank:{rank}",
            human_resolutions,
        )

    source = next(
        (
            placement
            for placement in placements
            if (
                isinstance(placement.get("rank"), int)
                and placement["rank"] < rank
                and rank
                < placement["rank"] + len(placement.get("submission_ids", []))
                and len(placement.get("submission_ids", [])) > 1
            )
        ),
        None,
    )
    if not isinstance(source, dict):
        return [], None

    source_rank = int(source["rank"])
    remaining = [
        str(submission_id) for submission_id in source.get("submission_ids", [])
    ]
    for prior_rank in range(source_rank, rank):
        prior_key = f"rank:{prior_rank}"
        selected = human_resolutions.get(prior_key)
        if selected:
            if selected not in remaining:
                raise ConfigValidationError(
                    f"Tie resolution '{prior_key}={selected}' does not select "
                    "a remaining project in the tied group."
                )
            remaining.remove(selected)
        elif len(remaining) == 1:
            remaining.pop()
        else:
            raise ConfigValidationError(
                "The event's human-resolution tie policy requires "
                f"`--tie-resolution {prior_key}=<submission-id>` before "
                f"placement {rank} can be assigned."
            )

    key = f"rank:{rank}"
    selected = human_resolutions.get(key)
    explicitly_selected = bool(selected)
    if selected:
        if selected not in remaining:
            raise ConfigValidationError(
                f"Tie resolution '{key}={selected}' does not select a remaining "
                "project in the tied group."
            )
    elif len(remaining) == 1:
        selected = remaining[0]
    else:
        raise ConfigValidationError(
            "The event's human-resolution tie policy requires "
            f"`--tie-resolution {key}=<submission-id>`."
        )
    return [selected], {
        "key": key,
        "mode": "human-resolution",
        "candidate_submission_ids": sorted(remaining),
        "selected_submission_ids": [selected],
        "resolution": (
            "human-declared"
            if explicitly_selected
            else "human-resolution-derived"
        ),
        "source_rank": source_rank,
    }


def _load_awards(bundle_path: Path) -> Optional[Dict]:
    path = bundle_path / "winner" / "awards.json"
    if not path.exists():
        return None
    return load_json(path)


def _choose_award_winners(
    bundle_path: Path,
    builder_winner_id: Optional[str],
    clock: Optional[Callable] = None,
    tie_resolutions: Optional[Dict[str, str]] = None,
) -> Dict:
    submissions = {s["submission_id"]: s for s in _load_submissions(bundle_path)}
    verdicts = {v["submission_id"]: v for v in _load_verdicts(bundle_path)}
    shadow = load_shadow_score(bundle_path) or {}
    tie_policy = _sealed_tie_policy(bundle_path, shadow)
    human_resolutions = dict(tie_resolutions or {})
    ranking = list(shadow.get("ranking") or [])
    if not ranking:
        ranking = sorted(
            verdicts,
            key=lambda sid: (-float(verdicts[sid].get("total_score", 0)), sid),
        )
    if not ranking:
        ranking = list(submissions)
    placements = _shadow_placements(shadow, ranking)

    feedback = {f.get("submission_id"): f for f in _load_feedback(bundle_path)}
    award_tie_resolutions: List[Dict[str, Any]] = []

    def picks_for(award: Dict) -> List[str]:
        placement = award.get("rank")
        if isinstance(placement, int) and not isinstance(placement, bool):
            matching = next(
                (
                    item["submission_ids"]
                    for item in placements
                    if item["rank"] == placement
                ),
                [],
            )
            if tie_policy["mode"] == "human-resolution":
                selected, resolution = _resolve_human_rank_placement(
                    placements,
                    placement,
                    human_resolutions,
                )
                selected = [sid for sid in selected if sid in submissions]
            else:
                selected, resolution = _resolve_award_tie(
                    [sid for sid in matching if sid in submissions],
                    verdicts,
                    tie_policy,
                    f"rank:{placement}",
                    human_resolutions,
                )
            if resolution:
                resolution.update(
                    {
                        "award_id": award["id"],
                        "award_name": award["name"],
                        "placement": placement,
                    }
                )
                award_tie_resolutions.append(resolution)
            return selected
        if not award.get("dimensions") and ranking:
            first_place = next(
                (
                    item["submission_ids"]
                    for item in placements
                    if item["rank"] == 1
                ),
                [ranking[0]],
            )
            selected, resolution = _resolve_award_tie(
                [sid for sid in first_place if sid in submissions],
                verdicts,
                tie_policy,
                "rank:1",
                human_resolutions,
            )
            if resolution:
                resolution.update(
                    {
                        "award_id": award["id"],
                        "award_name": award["name"],
                        "placement": 1,
                    }
                )
                award_tie_resolutions.append(resolution)
            return selected
        candidates: List[tuple[float, str]] = []
        for sid, verdict in verdicts.items():
            candidates.append(
                (_dimension_score(verdict, award.get("dimensions", [])), sid)
            )
        if not candidates:
            return [ranking[0] if ranking else next(iter(submissions))]
        best_score = max(score for score, _ in candidates)
        tied_ids = sorted(
            sid
            for score, sid in candidates
            if math.isclose(score, best_score, abs_tol=1e-9)
        )
        selected, resolution = _resolve_award_tie(
            tied_ids,
            verdicts,
            tie_policy,
            f"award:{award['id']}",
            human_resolutions,
        )
        if resolution:
            resolution.update(
                {
                    "award_id": award["id"],
                    "award_name": award["name"],
                    "placement": award.get("rank"),
                }
            )
            award_tie_resolutions.append(resolution)
        return selected

    awards: List[Dict] = []
    for award in _event_awards(bundle_path):
        selected_ids = picks_for(award)
        shared_placement = len(selected_ids) > 1
        for sid in selected_ids:
            sub = submissions.get(sid, {})
            verdict = verdicts.get(sid, {})
            fb = feedback.get(sid, {})
            award_criterion = award.get(
                "reason",
                "This project stood out through a strong response to the event rubric.",
            )
            bright_spot = _compact_text(fb.get("bright_spot"))
            reason = award_criterion
            if bright_spot:
                reason = f"{award_criterion} The panel highlighted: {bright_spot}"
            awards.append({
                "award_id": award["id"],
                "award_name": award["name"],
                "emoji": award["emoji"],
                "tagline": award["tagline"],
                "placement": award.get("rank"),
                "shared_placement": shared_placement,
                "winner_submission_id": sid,
                "winner_builder_name": sub.get("builder_name", verdict.get("builder_name", "Unknown")),
                "project_name": sub.get("project_name", verdict.get("project_name", "Unknown")),
                "reason": reason,
                "selection_basis": {
                    "award_criterion": award_criterion,
                    "judges_liked": fb.get("judges_liked", []),
                    "tie_policy": tie_policy["mode"],
                },
                "score": float(verdict.get("total_score", 0)),
            })

    return {
        "run_id": load_manifest(bundle_path).get("run_id", bundle_path.name),
        "declared_at": _now(clock),
        "requires_human_approval": True,
        "published": False,
        "awards": awards,
        "tie_ceremony": {
            "policy": tie_policy,
            "score_tie_events": shadow.get("tie_events", []),
            "award_tie_resolutions": award_tie_resolutions,
        },
    }


def _write_awards_markdown(bundle_path: Path, awards_card: Dict) -> None:
    lines = [f"# {_event_name(bundle_path)} Awards", ""]
    tie_ceremony = awards_card.get("tie_ceremony", {})
    policy = tie_ceremony.get("policy", {}) if isinstance(tie_ceremony, dict) else {}
    if isinstance(policy, dict):
        lines += [
            f"**Tie policy:** {policy.get('mode', 'shared-podium')}",
            "",
        ]
    for award in awards_card.get("awards", []):
        lines += [
            f"## {award.get('emoji', '🏆')} {award.get('award_name', 'Award')}",
            "",
            f"**Project:** {award.get('project_name', 'Unknown')}  ",
            f"**Built by:** {award.get('winner_builder_name', 'Unknown')}  ",
            "",
            award.get("tagline", ""),
            "",
            f"**Why it stood out:** {award.get('reason', '')}",
            "",
        ]
        if award.get("shared_placement"):
            lines += [
                "**Podium result:** Shared placement under the declared tie policy.",
                "",
            ]
        selection_basis = award.get("selection_basis", {})
        if selection_basis.get("award_criterion"):
            lines += [
                f"**Award lens:** {selection_basis['award_criterion']}",
                "",
            ]
        highlights = selection_basis.get("judges_liked", [])
        if highlights:
            lines += ["**What the judges liked:**", ""]
            for highlight in highlights:
                if not isinstance(highlight, dict):
                    continue
                lens = highlight.get("lens", "Panel lens")
                text = highlight.get("highlight", "")
                if text:
                    lines.append(f"- **{lens}:** {text}")
            lines.append("")
    lines += ["> Generated by Hackathon Judge. Human approval is required before external publishing.", ""]
    write_once(bundle_path / "winner" / "awards.md", "\n".join(lines))


def _tie_ceremony_notes(awards_card: Dict) -> List[str]:
    """Turn sealed tie artifacts into clear, non-score ceremony language."""
    tie_ceremony = awards_card.get("tie_ceremony", {})
    if not isinstance(tie_ceremony, dict):
        return []
    notes: List[str] = []
    covered_keys = set()
    award_events = tie_ceremony.get("award_tie_resolutions", [])
    if isinstance(award_events, list):
        def event_order(event: Dict[str, Any]) -> tuple[int, bool]:
            rank = event.get("source_rank", event.get("placement"))
            return (
                rank if isinstance(rank, int) else 0,
                event.get("resolution") == "human-resolution-derived",
            )

        ordered_events = sorted(
            (event for event in award_events if isinstance(event, dict)),
            key=event_order,
        )
        for event in ordered_events:
            selected = event.get("selected_submission_ids", [])
            if not isinstance(selected, list) or len(selected) < 1:
                continue
            resolution = event.get("resolution")
            key = event.get("key")
            if isinstance(key, str):
                covered_keys.add(key)
            award_name = str(event.get("award_name") or "podium placement")
            if resolution == "shared-podium":
                notes.append(
                    f"Shared podium: {len(selected)} projects share {award_name}. "
                    "The next numbered placement advances under the declared policy."
                )
            elif resolution == "human-declared":
                notes.append(
                    f"The tied {award_name} placement was resolved by the event's "
                    "logged human decision; the sealed panel result was not changed."
                )
            elif resolution == "human-resolution-derived":
                notes.append(
                    f"The remaining tied project advances to {award_name} under "
                    "the logged human resolution."
                )
            elif resolution == "sealed-tiebreaker":
                notes.append(
                    f"A tie for {award_name} was resolved by its predeclared sealed "
                    "tiebreaker before the reveal."
                )

    score_events = tie_ceremony.get("score_tie_events", [])
    awarded_ranks = {
        award.get("placement")
        for award in awards_card.get("awards", [])
        if isinstance(award, dict) and isinstance(award.get("placement"), int)
    }
    if isinstance(score_events, list):
        for event in score_events:
            if not isinstance(event, dict):
                continue
            rank = event.get("rank")
            key = f"rank:{rank}" if isinstance(rank, int) else ""
            if (
                key in covered_keys
                or rank not in awarded_ranks
                or event.get("resolution") != "sealed-tiebreaker"
            ):
                continue
            notes.append(
                "A public tie reached the predeclared sealed tiebreaker; "
                "its result was locked before the ceremony."
            )
    return list(dict.fromkeys(notes))


def _print_quiet_award_results(awards: List[Dict]) -> None:
    """Render operator-friendly results without Live Show emcee language."""
    print("Award results")
    for award in awards:
        print(
            f"- {award.get('award_name', 'Award')}: "
            f"{award.get('project_name', 'Unknown')} "
            f"({award.get('winner_builder_name', 'Unknown')})"
        )
        if award.get("reason"):
            print(f"  Why selected: {award['reason']}")


def _print_award_ceremony(awards_card: Dict, args: Optional[argparse.Namespace] = None) -> None:
    awards = awards_card.get("awards", [])
    if not _showtime_enabled(args):
        _print_quiet_award_results(awards)
        return

    width = min(76, _terminal_width(max_width=80))
    _sideline("The panel did the thinking. The room gets the cheering.", "🎙️", "magenta")
    for note in _tie_ceremony_notes(awards_card):
        _sideline(note, "⚖️", "cyan")
    _drumroll(
        f"{len(awards)} award{'s' if len(awards) != 1 else ''}. "
        "Every builder moment is ready.",
        args,
    )
    for idx, award in enumerate(awards, 1):
        if awards:
            _sideline(f"Envelope {idx}/{len(awards)}", "✉️", "gold")
        if idx == len(awards):
            _audience_reveal_moment(args)
            _countdown_reveal(args)
        else:
            _sideline("Opening the next envelope.", "✉️", "gold")
        _showtime_pause(args, 0.6)

        emoji = award.get("emoji", "🏆")
        name = award.get("award_name", "Award").upper()
        if award.get("shared_placement"):
            name = f"{name} — SHARED"
        project = award.get("project_name", "Unknown")
        builder = award.get("winner_builder_name", "Unknown")
        tagline = award.get("tagline", "")
        reason = award.get("reason", "")

        # Bordered winner card
        print()
        print(_paint("╔" + "═" * width + "╗", "magenta", bold=True))
        print(_paint("║" + f"  {emoji}  {name}".center(width) + "║", "magenta", bold=True))
        print(_paint("╠" + "═" * width + "╣", "magenta", bold=True))
        print(_paint("║" + f"  📦 Project: {project}".ljust(width) + "║", "gold", bold=True))
        print(_paint("║" + f"  👥 Built by: {builder}".ljust(width) + "║", "cyan"))
        if getattr(args, "operator", False) and award.get("score") is not None:
            score = float(award["score"])
            print(_paint("║  📊 Score:   ", "cyan") + _paint(f"{score:.1f}/10  ", "gold", bold=True) + _score_bar(score) + _paint(" " * 2 + "║", "cyan"))
        if tagline:
            print(_paint("║" + f"  \"{tagline}\"".ljust(width) + "║", "cyan"))
        if reason:
            for line in [reason[i:i+width-4] for i in range(0, len(reason), width-4)]:
                print(_paint("║" + f"  ✨ {line}".ljust(width) + "║", "green"))
        print(_paint("╚" + "═" * width + "╝", "magenta", bold=True))
        _showtime_pause(args, 0.4)


def _share_card(awards_card: Dict, run_id: str) -> None:
    awards = awards_card.get("awards", [])
    if not awards:
        return
    width = min(76, _terminal_width(max_width=80))
    print()
    print(_paint("┌" + "─" * width + "┐", "blue", bold=True))
    print(_paint("│" + "📣  SHARE THIS MOMENT".center(width) + "│", "gold", bold=True))
    print(_paint("│" + _truncate(f"Hackathon Judge · {run_id}", width - 4).center(width) + "│", "cyan"))
    print(_paint("│" + " " * width + "│", "blue"))
    for award in awards:
        line = (
            f"{award.get('emoji', '🏆')} {award.get('award_name', 'Award').replace('Copilot ', ''):<20}"
            f"→ {award.get('project_name', 'Unknown')} · {award.get('winner_builder_name', 'Unknown')}"
        )
        print(_paint("│  " + _truncate(line, width - 4).ljust(width - 2) + "│", "green"))
    print(_paint("│" + " " * width + "│", "blue"))
    replay_line = f"Replay this exact run: python3 hackathon_judge.py replay {run_id}"
    print(_paint("│  " + _truncate(replay_line, width - 4).ljust(width - 2) + "│", "cyan"))
    print(_paint("└" + "─" * width + "┘", "blue", bold=True))


def _print_workshop_receipt(bundle_path: Path, run_id: str) -> None:
    awards_card = _load_awards(bundle_path) or {}
    awards = awards_card.get("awards", [])
    verdicts = _load_verdicts(bundle_path)
    feedback = _load_feedback(bundle_path)
    gate_path = bundle_path / "freshness_gate.json"
    gate = load_json(gate_path) if gate_path.exists() else {}
    archive = bundle_path.parent / f"{run_id}.bundle.tar.gz"
    bundle_sealed = (
        (bundle_path / "SEAL").exists()
        and (bundle_path / "HASHES").exists()
        and archive.exists()
    )

    envelope_status = "envelope sealed" if bundle_sealed else "export pending"
    _magic_banner(
        "Workshop Recap",
        f"{len(verdicts)} repos · {len(awards)} awards · {envelope_status}",
    )
    if awards:
        for award in awards:
            print(_paint(
                f"{award.get('emoji', '🏆')} {award.get('award_name')}: "
                f"{award.get('project_name')} ({award.get('winner_builder_name')})",
                "gold",
                bold=True,
            ))
    print()
    print(_paint("📊 Room energy", "magenta", bold=True))
    print(_paint(f"   Repos judged:        {len(verdicts)}", "cyan"))
    print(_paint(f"   Bright spots found:  {len(feedback)}", "cyan"))
    print(
        _paint(
            f"   Review panel:        {_model_panel_label(gate)} "
            f"({gate.get('status', 'sealed')})",
            "cyan",
        )
    )
    if bundle_sealed:
        print(_paint("   Score envelope:      sealed and replayable", "green", bold=True))
    else:
        print(_paint("   Score envelope:      awarded; export pending", "yellow", bold=True))
    print()
    print(_paint(f"📦 Bundle: {bundle_path}", "blue"))
    if archive.exists():
        print(_paint(f"📼 Replay archive: {archive}", "blue"))
    if (bundle_path / "recap.md").exists():
        print(_paint(f"📝 Recap: {bundle_path / 'recap.md'}", "blue"))
    if awards_card.get("requires_human_approval", True):
        print(_paint("⚠️  Human approval required before external publishing.", "yellow"))
    if bundle_sealed:
        _share_card(awards_card, run_id)
    else:
        print(_paint(
            f"⚠️  Run 'hackathon-judge export {run_id}' before treating this result as tamper-evident.",
            "yellow",
        ))


def _run_workshop_tail_step(label: str, detail: str, fn: Callable,
                            ns: argparse.Namespace, showtime: bool,
                            _gateway: Optional[Any],
                            clock: Optional[Callable]) -> int:
    if not showtime:
        _sideline(f"Running {label}...", "⬢", "cyan")
        return fn(ns, _gateway, clock)

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = fn(ns, _gateway, clock)
    if rc:
        print(stdout.getvalue(), end="")
        print(stderr.getvalue(), end="", file=sys.stderr)
        return rc
    print(_paint(f"   ✓ {label:<18} {detail}", "green", bold=True))
    return 0


def _seal_the_night(bundle_path: Path, run_id: str, showtime: bool,
                    _gateway: Optional[Any], clock: Optional[Callable]) -> int:
    if showtime:
        _magic_banner("Sealing the Night", "Recap · export · validate · replay")
    steps = [
        ("Recap written", "recap.md", cmd_recap, argparse.Namespace(run_id=run_id, out=None)),
        ("Bundle exported", f"{run_id}.bundle.tar.gz", cmd_export, argparse.Namespace(run_id=run_id, force=False)),
        (
            "Bundle validated",
            "all artifacts intact",
            cmd_validate,
            argparse.Namespace(bundle=str(bundle_path.resolve())),
        ),
        (
            "Replay verified",
            "stored artifacts only",
            cmd_replay,
            argparse.Namespace(bundle=str(bundle_path.resolve()), showtime=False),
        ),
    ]
    for label, detail, fn, ns in steps:
        rc = _run_workshop_tail_step(label, detail, fn, ns, showtime, _gateway, clock)
        if rc:
            return rc
    if showtime:
        _sideline("Envelope sealed. This show is replayable forever.", "🔒", "green")
    return 0


@_with_showtime_pacing(_workshop_showtime_enabled)
def cmd_workshop(args: argparse.Namespace, _gateway: Optional[Any] = None,
                 clock: Optional[Callable] = None) -> int:
    """workshop — live facilitator flow from repo intake to award reveal."""
    started_at = time.monotonic()
    configure = bool(getattr(args, "configure", False))
    demo = bool(getattr(args, "demo", False))
    manual_confirm = bool(getattr(args, "manual_confirm", False))
    require_projector_window = bool(getattr(args, "require_projector_window", False))
    require_live_terminal = bool(
        getattr(args, "require_live_terminal", False) or require_projector_window
    )
    projector = bool(getattr(args, "projector", False))
    showtime = True if demo else _workshop_showtime_enabled(args)
    assume_yes = bool(getattr(args, "yes", False) or showtime or demo) and not manual_confirm
    gateway = None if demo else _gateway
    if require_live_terminal and not sys.stdout.isatty():
        _print_error(
            7,
            "ConfigValidationError",
            "The Live Show requires a real interactive terminal. Open one terminal, "
            "share that window, and rerun the same command.",
        )
        return 7
    if showtime:
        _set_terminal_title("Hackathon Judge — LIVE SHOW — SHARE THIS WINDOW")

    _magic_banner(
        "Hackathon Judge Live Show",
        "LIVE SHOW — SHARE THIS WINDOW",
    )
    _sideline(
        "Sideline report: one screen, every project, and a sealed final reveal.",
        "📡",
        "magenta",
    )
    if demo:
        _sideline(
            "Practice demo: the projects and results are illustrative, not official outcomes.",
            "🧪",
            "yellow",
        )

    run_prefix = "demo" if demo else "show"
    default_run = f"{run_prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    run_id = getattr(args, "run_id", None) or (default_run if not configure else _ask_text("Run name", default_run))

    audience = getattr(args, "audience", None) or (
        "external" if not configure else _ask_choice("Who is in the room?", ["external", "internal"], "external")
    )
    mode = "workshop" if audience == "external" else "async"

    panel_style = ("fun" if demo else getattr(args, "panel_style", None)) or (
        "fun" if not configure else _ask_choice("Panel style?", ["fun", "professional"], "fun")
    )

    url_text = "\n".join(getattr(args, "urls", None) or [])
    if getattr(args, "file", None):
        url_text += "\n" + Path(args.file).read_text(encoding="utf-8")
    if not url_text and not demo and not sys.stdin.isatty():
        url_text = sys.stdin.read()
    if demo and not url_text:
        entries = copy.deepcopy(DEMO_SUBMISSIONS)
    else:
        if not url_text:
            url_text = _ask_repo_block()
        entries = parse_submission_entries(url_text)
    if not entries:
        _print_error(7, "ConfigValidationError", "No GitHub repo URLs found for the Live Show.")
        return 7

    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)
    show_args = argparse.Namespace(
        run_id=run_id,
        showtime=showtime,
        no_suspense=getattr(args, "no_suspense", False),
        projector=projector,
    )

    if assume_yes:
        _sideline("Opening the hackathon room...", "🎬", "magenta")
    elif not _confirm("Create the workshop run bundle?"):
        return 0
    init_args = argparse.Namespace(
        run_id=run_id,
        mode=mode,
        config=getattr(args, "config", None),
        event=getattr(args, "event", None),
        showtime=showtime,
        quiet=showtime,
    )
    rc = cmd_init(init_args, gateway, clock)
    if rc:
        return rc

    event_spec = load_event_spec(bundle_path)
    active_awards = [
        award
        for award in event_spec["awards"]
        if not isinstance(award.get("rank"), int) or award["rank"] <= len(entries)
    ]
    awards = ", ".join(award["name"] for award in active_awards)
    _tonight_card(run_id, len(entries), awards, show_args)
    manifest = load_manifest(bundle_path)
    manifest["workshop_choices"] = {
        "audience": audience,
        "awards": [award["name"] for award in active_awards],
        "panel_style": panel_style,
        "showtime": showtime,
        "projector": projector,
        "display_surface": "single-terminal",
        "optional_monitor_auto_launched": False,
        "demo": demo,
        "audience_view": event_spec["presentation"]["audience_view"],
        "submission_count_requested": len(entries),
    }
    save_manifest(bundle_path, manifest)

    _sideline("This is the complete show. No second audience window will open.", "🖥️", "cyan")

    _act_break("ACT I — PROJECTS ENTER", show_args)
    if assume_yes:
        _sideline(
            f"From the floor: {len(entries)} project link(s) are through the doors.",
            "📋",
            "cyan",
        )
    elif not _confirm(f"Import {len(entries)} repo submission(s)?"):
        return 0
    created = import_url_submissions(
        bundle_path,
        entries,
        "Hackathon Participants",
        clock,
        metadata_provider=_demo_repo_metadata if demo else None,
    )
    log_command(bundle_path, "workshop-import", "ok", f"created={len(created)} urls={len(entries)}", clock)
    _magic_banner("Project Intake", f"{len(created)} projects entered · {len(entries) - len(created)} already present")
    _project_count_hero(len(created), show_args)
    for sub in created:
        meta = sub.get("repo_metadata", {})
        details = project_showcase_badges(meta)
        suffix = f" — {' · '.join(details)}" if details else ""
        _sideline(f"{sub['project_name']} enters the room{suffix}.", "🌟", "magenta")
        _showtime_pause(show_args, 0.35)

    _act_break("ACT II — THE PANEL SCORES", show_args)
    if assume_yes:
        _sideline(
            "Sideline report: the judges are locked in and the leaderboard is sealed.",
            "🏟️",
            "magenta",
        )
    elif not _confirm("Start judging?"):
        return 0
    rc = cmd_judge(
        argparse.Namespace(
            run_id=run_id,
            showtime=showtime,
            no_suspense=getattr(args, "no_suspense", False),
        ),
        gateway,
        clock,
    )
    if rc:
        return rc

    _act_break("ACT III — SPOTLIGHTS", show_args)
    if assume_yes:
        _sideline("Spotlight round. Every builder gets a moment.", "🎬", "gold")
    elif not _confirm("Open the spotlight round?"):
        return 0
    rc = cmd_present(
        argparse.Namespace(
            run_id=run_id,
            showtime=showtime,
            no_suspense=getattr(args, "no_suspense", False),
        ),
        gateway,
        clock,
    )
    if rc:
        return rc

    try:
        tie_resolutions = _parse_tie_resolutions(
            getattr(args, "tie_resolution", None)
        )
        winner_id = _winner_id_from_award_selection(
            bundle_path,
            tie_resolutions,
            clock,
        )
    except ConfigValidationError as exc:
        _print_error(exc.exit_code, type(exc).__name__, str(exc))
        return exc.exit_code
    if not winner_id:
        _print_error(7, "ConfigValidationError", "No winner could be selected.")
        return 7

    _act_break("ACT IV — AWARD REVEAL", show_args)
    if assume_yes:
        _sideline("The envelopes are sealed. Opening the awards.", "✉️", "gold")
    elif not _confirm("Reveal the award winners?"):
        return 0
    rc = cmd_award(argparse.Namespace(
        run_id=run_id,
        winner=winner_id,
        tie_resolution=getattr(args, "tie_resolution", None),
        showtime=showtime,
        no_suspense=getattr(args, "no_suspense", False),
    ), gateway, clock)
    if rc:
        return rc

    should_export = assume_yes or _confirm("Export, validate, recap, and replay the sealed run?")
    if should_export:
        _act_break("ACT V — SEALING THE NIGHT", show_args)
        rc = _seal_the_night(bundle_path, run_id, showtime, gateway, clock)
        if rc:
            return rc

    _print_workshop_receipt(bundle_path, run_id)
    _sideline(
        "Joy check: every builder got a spotlight, and the room brought the finish.",
        "🎉",
        "green",
    )
    elapsed_seconds = time.monotonic() - started_at
    if demo:
        _success(
            f"Practice Live Show complete in {elapsed_seconds:.1f}s "
            f"(budget: {DEMO_TIME_BUDGET_SECONDS:.0f}s): {run_id}"
        )
        if elapsed_seconds > DEMO_TIME_BUDGET_SECONDS:
            _warning("The practice demo exceeded its two-minute show budget.")
    else:
        _success(f"Live Show complete: {run_id}")
    return 0


def cmd_quick(args: argparse.Namespace, _gateway: Optional[Any] = None,
              clock: Optional[Callable] = None) -> int:
    """
    quick — quiet, private judging from project links to feedback proposals.

    This path intentionally omits emcee commentary, spotlights, countdowns,
    and public score output. It retains the same sealed artifacts as Live Show.
    """
    entries = _read_submission_entries_from_args(args)
    if not entries:
        _print_error(
            7,
            "ConfigValidationError",
            "No GitHub repo URLs found. Paste URLs, pass --file, or provide owner/repo entries.",
        )
        return 7

    default_run = f"quick-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    run_id = getattr(args, "run_id", None) or default_run
    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)
    init_args = argparse.Namespace(
        run_id=run_id,
        mode="async",
        config=getattr(args, "config", None),
        event=getattr(args, "event", None),
        showtime=False,
        quiet=True,
    )
    rc = cmd_init(init_args, _gateway, clock)
    if rc:
        return rc

    created = import_url_submissions(
        bundle_path,
        entries,
        getattr(args, "builder_name", "Hackathon Participants"),
        clock,
    )
    log_command(
        bundle_path,
        "quick-import",
        "ok",
        f"created={len(created)} urls={len(entries)}",
        clock,
    )
    manifest = load_manifest(bundle_path)
    manifest["engagement_mode"] = "quick"
    save_manifest(bundle_path, manifest)

    with contextlib.redirect_stdout(io.StringIO()):
        rc = cmd_judge(
            argparse.Namespace(run_id=run_id, showtime=False, no_suspense=True),
            _gateway,
            clock,
        )
    if rc:
        return rc

    try:
        tie_resolutions = _parse_tie_resolutions(
            getattr(args, "tie_resolution", None)
        )
        winner_id = _winner_id_from_award_selection(
            bundle_path,
            tie_resolutions,
            clock,
        )
    except ConfigValidationError as exc:
        _print_error(exc.exit_code, type(exc).__name__, str(exc))
        return exc.exit_code
    if not winner_id:
        _print_error(7, "ConfigValidationError", "No winner could be selected.")
        return 7

    award_output = io.StringIO()
    with contextlib.redirect_stdout(award_output):
        rc = cmd_award(
            argparse.Namespace(
                run_id=run_id,
                winner=winner_id,
                tie_resolution=getattr(args, "tie_resolution", None),
                showtime=False,
                no_suspense=True,
                quiet=True,
                operator=False,
            ),
            _gateway,
            clock,
        )
    if rc:
        print(award_output.getvalue(), end="", file=sys.stderr)
        return rc

    proposals = _build_feedback_proposals(bundle_path, clock=clock)
    proposal_path = _write_feedback_proposals(runs_dir, run_id, proposals)
    seal_stdout = io.StringIO()
    seal_stderr = io.StringIO()
    with contextlib.redirect_stdout(seal_stdout), contextlib.redirect_stderr(seal_stderr):
        rc = _seal_the_night(bundle_path, run_id, False, _gateway, clock)
    if rc:
        print(seal_stdout.getvalue(), end="")
        print(seal_stderr.getvalue(), end="", file=sys.stderr)
        if (bundle_path / "SEAL").exists() and not (
            runs_dir / f"{run_id}.bundle.tar.gz"
        ).exists():
            print(
                f"Resume the partial export with: python3 hackathon_judge.py export {run_id}",
                file=sys.stderr,
            )
        return rc
    awards_card = _load_awards(bundle_path) or {}
    freshness_gate = load_json(bundle_path / "freshness_gate.json")
    provenance = freshness_gate.get("evaluation_provenance", {})
    evaluation_mode = provenance.get("mode", "unknown")

    print(f"Quick judging complete: {run_id}")
    print(f"Projects reviewed: {len(created)}")
    if evaluation_mode == "simulated":
        print("Evaluation mode: simulated — practice demo only; do not publish these as official awards.")
    else:
        print(
            f"Evaluation mode: {evaluation_mode} · "
            f"panel: {_model_panel_label(freshness_gate)}"
        )
    _print_quiet_award_results(awards_card.get("awards", []))
    print(f"Run bundle: {bundle_path}")
    print("Validation: passed (HASHES and SEAL verified).")
    print(f"Replay: python3 hackathon_judge.py replay {run_id}")
    print(f"Private project feedback: {proposal_path}")
    print("Human approval is required before delivering feedback externally.")
    return 0


@_with_showtime_pacing()
def cmd_judge(args: argparse.Namespace, _gateway: Optional[Any] = None,
              clock: Optional[Callable] = None) -> int:
    """judge — trigger eval engine; freshness gate + scoring + shadow score seal."""
    run_id = args.run_id
    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)

    _assert_bundle_exists(bundle_path, run_id)
    manifest = load_manifest(bundle_path)
    gate_path = bundle_path / "freshness_gate.json"

    if gate_path.exists() and manifest.get("status") == "sealed":
        print(f"[INFO] Run '{run_id}' is already judged and sealed. Use 'resume' for incomplete runs.", file=sys.stderr)
        return 0

    _assert_status_in(manifest, ["init", "collecting", "judging"], "judge")
    rubric = load_rubric(bundle_path)
    event_spec = load_event_spec(bundle_path)
    panel_style = _panel_style_for(manifest)

    showtime = _showtime_enabled(args)
    evaluation_started_at = time.monotonic()
    stage_seconds: Dict[str, float] = {}

    # Step 2: Freshness gate
    if showtime:
        _magic_banner(event_spec["event"]["name"], event_spec["event"]["tagline"])
        _sideline("Review lenses are ready. Scores stay sealed until the award reveal.", "🏟️", "magenta")
        _sideline(_panel_opening_message(event_spec, panel_style), "🎙️", "magenta")
    else:
        _magic_banner(event_spec["event"]["name"], "Premium model policy, sealed scores, and fair review.")
        _sideline("The judging panel is warming up.", "🏟️", "magenta")
    _showtime_pause(args)
    if showtime:
        _sideline("Freshness Gate opening...", "🧭", "cyan")
    else:
        _step(1, 7, "Running Model Freshness Gate...", "🧭")
    stage_started_at = time.monotonic()
    try:
        gate_result = run_freshness_gate(bundle_path, rubric, _gateway, clock)
    except FreshnessGateBlock as e:
        log_command(bundle_path, "judge", "blocked", str(e), clock)
        _print_error(3, "FreshnessGateBlock", str(e))
        return 3
    except ModelAPIError as e:
        log_command(bundle_path, "judge", "error", str(e), clock)
        _print_error(8, "ModelAPIError", str(e))
        return 8
    stage_seconds["freshness_gate"] = time.monotonic() - stage_started_at

    selected_model = gate_result["selected_model"]
    selected_models = gate_result.get("selected_models") or [selected_model]
    panel_label = f"{len(selected_models)}-model consensus panel"
    provenance = gate_result.get("evaluation_provenance", {})
    if showtime:
        if provenance.get("mode") == "simulated":
            _sideline(
                "Evaluation mode: simulated — practice demo only; awards are illustrative.",
                "🧠",
                "yellow",
            )
        else:
            _sideline(
                f"Evaluation mode: {provenance.get('mode', 'unknown')} · {panel_label}.",
                "🧠",
                "green",
            )
    else:
        _sideline(
            f"Freshness Gate: {gate_result['status']} — {panel_label}",
            "🧠",
            "green",
        )
    _showtime_pause(args)

    # Load submissions
    submissions = _load_submissions(bundle_path)
    if not submissions:
        _print_error(7, "ConfigValidationError", "No submissions found. Use 'submit' first.")
        return 7

    update_status(bundle_path, "judging", clock)
    evaluation_plan = _write_evaluation_plan(
        bundle_path, submissions, rubric, selected_models, clock
    )
    calls = evaluation_plan["calls"]
    scoring_calls_per_submission = (
        evaluation_plan["review_lens_count"] * evaluation_plan["panel_model_count"]
    )
    shadow_calls_per_submission = evaluation_plan["panel_model_count"]
    _write_evaluation_progress(
        bundle_path,
        evaluation_plan,
        "shadow-spec",
        0,
        remaining_model_calls=calls["total"],
        clock=clock,
    )
    if showtime:
        _sideline(
            f"{calls['total']} panel checks queued; up to "
            f"{evaluation_plan['max_parallel_calls']} run at once.",
            "⏱",
            "blue",
        )

    # Seal the diagnostic criteria before any project gets a public score.
    if showtime:
        _sideline(
            "The hidden quality mesh is sealed. It will not change the podium.",
            "🔍",
            "blue",
        )
    else:
        _step(2, 7, "Sealing diagnostic Shadow Spec...", "🔍")
    stage_started_at = time.monotonic()
    try:
        shadow_spec = generate_shadow_spec(
            bundle_path,
            rubric,
            selected_models,
            _gateway,
            clock,
        )
    except (ConfigValidationError, ModelAPIError) as e:
        _write_evaluation_progress(
            bundle_path,
            evaluation_plan,
            "shadow-spec",
            0,
            status="failed",
            remaining_model_calls=calls["total"],
            clock=clock,
        )
        _print_error(e.exit_code, type(e).__name__, str(e))
        return e.exit_code
    stage_seconds["shadow_spec"] = time.monotonic() - stage_started_at
    _showtime_pause(args)

    # Step 3: Determine completed eval steps (for resume)
    completed_sids = set()
    for step_file in sorted((bundle_path / "eval").glob("step_*.json")):
        step = load_json(step_file)
        completed_sids.add(step.get("submission_id"))

    remaining = [s for s in submissions if s["submission_id"] not in completed_sids]
    already_scored = []
    if completed_sids:
        for step_file in sorted((bundle_path / "eval").glob("step_*.json")):
            step = load_json(step_file)
            already_scored.append(step["scored_submission"])

    _write_evaluation_progress(
        bundle_path,
        evaluation_plan,
        "public-scoring",
        len(already_scored),
        remaining_model_calls=(
            len(remaining) * scoring_calls_per_submission
            + len(submissions) * shadow_calls_per_submission
        ),
        clock=clock,
    )
    if showtime:
        _sideline(f"Scoring the room: {len(remaining)} build(s) under the lights.", "⚖️", "gold")
    else:
        _step(3, 7, f"Scoring {len(remaining)} submission(s) with the panel...", "⚖️")
    _showtime_pause(args)
    milestones = _showcase_milestones(len(remaining))
    scoring_started_at = time.monotonic()

    def progress(sub: Dict, scored: Dict, index: int, total: int) -> None:
        elapsed = time.monotonic() - scoring_started_at
        remaining_seconds = max(0, round((elapsed / index) * (total - index)))
        _write_evaluation_progress(
            bundle_path,
            evaluation_plan,
            "public-scoring",
            len(already_scored) + index,
            estimated_remaining_seconds=remaining_seconds,
            remaining_model_calls=(
                (total - index) * scoring_calls_per_submission
                + len(submissions) * shadow_calls_per_submission
            ),
            clock=clock,
        )
        if not showtime or index not in milestones:
            return
        name = _truncate(str(sub.get("project_name", sub.get("submission_id", "Project"))), 38)
        print(_paint(f"   ⬢ {name}", "cyan", bold=True))
        _showtime_pause(args, 0.2)
        eta = f" · about {remaining_seconds}s left" if total > index else ""
        print(_paint(f"     Review sealed  [{index}/{total}]{eta}", "green"))
        _sideline(_panel_progress_message(index, total, panel_style), "🎙️", "magenta")
        _showtime_pause(args, 0.2)

    try:
        new_scored = score_submissions(
            remaining,
            rubric,
            selected_models,
            bundle_path,
            _gateway,
            clock,
            progress=progress,
        )
    except ModelAPIError as e:
        _write_evaluation_progress(
            bundle_path,
            evaluation_plan,
            "public-scoring",
            len(already_scored),
            status="failed",
            remaining_model_calls=(
                len(remaining) * scoring_calls_per_submission
                + len(submissions) * shadow_calls_per_submission
            ),
            clock=clock,
        )
        _print_error(8, "ModelAPIError", str(e))
        return 8
    stage_seconds["public_scoring"] = time.monotonic() - scoring_started_at

    all_scored = already_scored + new_scored
    _write_evaluation_progress(
        bundle_path,
        evaluation_plan,
        "shadow-analysis",
        len(all_scored),
        remaining_model_calls=len(submissions) * shadow_calls_per_submission,
        clock=clock,
    )

    # Step 4: Calculate the public ranking envelope in memory.
    if showtime:
        _sideline("The public-ranking envelope is ready to seal.", "🔒", "green")
    else:
        _step(4, 7, "Calculating the sealed ranking envelope...", "🔒")
    _showtime_pause(args)
    shadow = compute_shadow_score(all_scored, rubric, clock)

    # Step 5: Run the sealed diagnostic mesh. It is explicitly excluded from
    # the ranking above, so it can surface quality risks without changing awards.
    if showtime:
        _sideline(
            "Shadow analysis is checking the work. The podium stays independent.",
            "🔍",
            "blue",
        )
    else:
        _step(5, 7, "Running diagnostic Shadow Analysis...", "🔍")
    stage_started_at = time.monotonic()
    try:
        assess_shadow_spec(
            all_scored,
            submissions,
            shadow_spec,
            selected_models,
            bundle_path,
            _gateway,
            clock,
            rubric=rubric,
        )
    except ModelAPIError as e:
        _write_evaluation_progress(
            bundle_path,
            evaluation_plan,
            "shadow-analysis",
            len(all_scored),
            status="failed",
            remaining_model_calls=len(submissions) * shadow_calls_per_submission,
            clock=clock,
        )
        _print_error(e.exit_code, type(e).__name__, str(e))
        return e.exit_code
    stage_seconds["shadow_assessment"] = time.monotonic() - stage_started_at
    _write_evaluation_progress(
        bundle_path,
        evaluation_plan,
        "ranking-seal",
        len(all_scored),
        remaining_model_calls=0,
        clock=clock,
    )

    # The public envelope is sealed only after all adjacent sealed artifacts
    # have been written, because this closes the sealed/ directory.
    try:
        stage_started_at = time.monotonic()
        seal_shadow_score(bundle_path, shadow, clock)
    except BundleSealError as e:
        # Already sealed — that's fine if we're resuming
        pass
    stage_seconds["ranking_seal"] = time.monotonic() - stage_started_at
    _write_evaluation_progress(
        bundle_path,
        evaluation_plan,
        "verdicts",
        len(all_scored),
        remaining_model_calls=0,
        clock=clock,
    )

    # Step 5: Build panel verdicts
    if showtime:
        _sideline("Judge reactions locking in for every builder.", "🎙️", "magenta")
    else:
        _step(6, 7, "Writing judge reactions...", "🎙️")
    _showtime_pause(args)
    existing_verdict_sids = {p.stem for p in (bundle_path / "verdicts").glob("*.json")}
    remaining_for_verdicts = [s for s in all_scored if s["submission_id"] not in existing_verdict_sids]
    stage_started_at = time.monotonic()
    try:
        build_panel_verdicts(remaining_for_verdicts, submissions, rubric, selected_models,
                             bundle_path, _gateway, clock)
    except (ToneSafetyFailure, ModelAPIError) as e:
        _write_evaluation_progress(
            bundle_path,
            evaluation_plan,
            "verdicts",
            len(all_scored),
            status="failed",
            remaining_model_calls=0,
            clock=clock,
        )
        _print_error(e.exit_code, type(e).__name__, str(e))
        return e.exit_code
    stage_seconds["verdicts"] = time.monotonic() - stage_started_at

    # Step 6: Build feedback cards
    _write_evaluation_progress(
        bundle_path,
        evaluation_plan,
        "feedback",
        len(all_scored),
        remaining_model_calls=0,
        clock=clock,
    )
    if showtime:
        _sideline(
            "Bright spots, Copilot next moves, and frontier experiments are ready.",
            "✨",
            "gold",
        )
    else:
        _step(7, 7, "Preparing feedback and improvement experiments...", "✨")
    _showtime_pause(args)
    existing_fb_sids = {p.stem for p in (bundle_path / "feedback").glob("*.json")}
    remaining_for_feedback = [s for s in all_scored if s["submission_id"] not in existing_fb_sids]
    stage_started_at = time.monotonic()
    try:
        build_feedback_cards(remaining_for_feedback, submissions, rubric, selected_models,
                             bundle_path, _gateway, clock)
    except (ToneSafetyFailure, ModelAPIError) as e:
        _write_evaluation_progress(
            bundle_path,
            evaluation_plan,
            "feedback",
            len(all_scored),
            status="failed",
            remaining_model_calls=0,
            clock=clock,
        )
        _print_error(e.exit_code, type(e).__name__, str(e))
        return e.exit_code
    stage_seconds["feedback"] = time.monotonic() - stage_started_at

    update_status(bundle_path, "sealed", clock)
    timing = _write_evaluation_timing(
        bundle_path, evaluation_plan, stage_seconds, evaluation_started_at, clock
    )
    _write_evaluation_progress(
        bundle_path,
        evaluation_plan,
        "complete",
        len(all_scored),
        status="complete",
        remaining_model_calls=0,
        clock=clock,
    )
    log_command(bundle_path, "judge", "ok", f"scored={len(all_scored)}", clock)
    _success(
        f"Judging complete. {len(all_scored)} submission(s) scored and sealed "
        f"by a {panel_label}."
    )
    if showtime:
        _sideline("Quick huddle complete. Every project gets one clean spotlight next.", "🎙️", "magenta")
        if timing["budget_exceeded"]:
            _sideline(
                "The time budget passed, but every panel member completed the review.",
                "⏱",
                "yellow",
            )
    _sideline("The panel has spoken. The reveal is ready.", "🏁", "gold")
    return 0


@_with_showtime_pacing(_present_showtime_enabled)
def cmd_present(args: argparse.Namespace, _gateway: Optional[Any] = None,
                clock: Optional[Callable] = None) -> int:
    """present — generate presentation from stored artifacts only; no live calls."""
    run_id = args.run_id
    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)

    _assert_bundle_exists(bundle_path, run_id)
    manifest = load_manifest(bundle_path)
    _assert_status_in(manifest, ["sealed", "awarded", "exported"], "present")

    submissions = _load_submissions(bundle_path)
    sub_map = {s["submission_id"]: s for s in submissions}
    feedback = {f.get("submission_id"): f for f in _load_feedback(bundle_path)}
    showtime = _present_showtime_enabled(args)
    event_spec = load_event_spec(bundle_path)
    panel_style = _panel_style_for(manifest)
    audience_locked = manifest.get("status") not in {"awarded", "exported"}
    show_scores = bool(getattr(args, "operator", False)) and manifest.get("status") in {
        "awarded",
        "exported",
    }

    _magic_banner(
        event_spec["event"]["name"],
        f"Run: {run_id} · Mode: {manifest.get('mode', 'workshop').upper()}",
    )
    _sideline("The judges are seated. Every project gets a spotlight.", "🏟️", "magenta")
    _sideline("Scores remain hidden until the award reveal.", "🔒", "blue")
    if showtime:
        _sideline(
            "One fast panel take per project, then the envelopes.",
            "🎙️",
            "magenta" if panel_style == "fun" else "cyan",
        )
    _showtime_pause(args)

    # Load and display verdicts (NOT shadow scores)
    verdicts = _load_verdicts(bundle_path)
    if not verdicts:
        print("[INFO] No verdicts found. Run 'judge' first.")
        return 0

    spotlight_milestones = _showcase_milestones(len(verdicts))
    for spotlight_index, v in enumerate(verdicts, 1):
        score = float(v.get("total_score", 0))
        sid = v.get("submission_id")
        sub = sub_map.get(sid, {})
        meta = sub.get("repo_metadata", {})
        badges = project_showcase_badges(meta)
        if meta.get("contributors"):
            badges.append(f"👥 {meta['contributors']}")
        if meta.get("open_issues") is not None:
            badges.append(f"📌 {meta['open_issues']} issues")
        project = _truncate(str(v.get("project_name", sid)), 68)
        width = min(76, _terminal_width(max_width=80))
        print()
        print(_paint(f"┌─ 🌟 SPOTLIGHT: {project} ", "blue", bold=True) + _paint("─" * max(2, width - len(project) - 16), "blue"))
        print(_paint(f"│ Built by: {v.get('builder_name', 'Unknown')}", "cyan"))
        if meta.get("description"):
            print(_paint(f"│ What it does: {_truncate(str(meta.get('description')), 82)}", "cyan"))
        if badges:
            print(_paint(f"│ Project signals: {_truncate(' · '.join(badges), 78)}", "blue"))
        if meta.get("homepage"):
            print(_paint(f"│ Explore: {_truncate(str(meta['homepage']), 86)}", "blue"))
        if show_scores:
            print(_paint(f"│ Score:   {score:.2f}/10  {_score_bar(score)}", "gold", bold=True))
        if showtime:
            lens, take = _live_panel_take(v, panel_style, audience_locked)
            if panel_style == "professional":
                line = f"│ 🎙️ {lens}: {take}"
                print(_paint(_truncate(line, width), "cyan"))
            else:
                line = f"│ 🎙️ {lens} leans in: “{take}”"
                print(_paint(_truncate(line, width), "magenta"))
        else:
            for arch_v in v.get("archetype_verdicts", []):
                reaction = arch_v.get("bright_spot", arch_v.get("perspective", ""))
                if audience_locked:
                    reaction = _audience_safe_commentary(
                        reaction,
                        "The panel found a thoughtful detail worth celebrating.",
                    )
                print(_paint(f"│ 🎙️ {arch_v['archetype_name']}", "magenta", bold=True))
                print(_paint(f"│    {_truncate(reaction, 92)}", "green"))
        fb = feedback.get(v.get("submission_id"), {})
        if fb:
            for label, field, icon in (
                ("Copilot", "copilot_use", "🧠"),
                ("Frontier", "frontier_use", "🧭"),
            ):
                assessment = fb.get(field, {})
                if (
                    not isinstance(assessment, dict)
                    or assessment.get("status") != "evidenced"
                    or assessment.get("source") != "builder-provided"
                ):
                    continue
                evidence = str(assessment.get("summary", ""))
                if audience_locked:
                    evidence = _audience_safe_commentary(
                        evidence,
                        f"{label} context will be shared after the reveal.",
                    )
                print(_paint(f"│ {icon} {label}: {_truncate(evidence, 82)}", "blue"))
            if fb.get("bright_spot"):
                bright_spot = fb.get("bright_spot", "")
                if audience_locked:
                    bright_spot = _audience_safe_commentary(
                        bright_spot,
                        "This project brought a thoughtful moment to the room.",
                    )
                print(_paint(f"│ ✨ Bright Spot: {_truncate(bright_spot, 86)}", "green"))
            if fb.get("next_commit") and not showtime:
                next_commit = fb.get("next_commit", "")
                if audience_locked:
                    next_commit = _audience_safe_commentary(
                        next_commit,
                        "A helpful next step will be shared after the reveal.",
                    )
                print(_paint(f"│ 🔜 Next Commit: {_truncate(next_commit, 86)}", "yellow"))
            if not audience_locked:
                copilot_moves = fb.get("copilot_next_moves", [])
                if isinstance(copilot_moves, list) and copilot_moves:
                    print(
                        _paint(
                            f"│ 🧠 Copilot next: {_truncate(str(copilot_moves[0]), 82)}",
                            "blue",
                        )
                    )
                frontier_ideas = fb.get("frontier_experiments", [])
                if isinstance(frontier_ideas, list) and frontier_ideas:
                    print(
                        _paint(
                            f"│ 🧭 Frontier idea: {_truncate(str(frontier_ideas[0]), 82)}",
                            "blue",
                        )
                    )
        print(_paint("└" + "─" * width, "blue"))
        if spotlight_index in spotlight_milestones:
            _showtime_pause(args, 0.35)

    # Show winner if awarded
    awards_card = _load_awards(bundle_path)
    if not audience_locked and awards_card:
        print()
        _print_award_ceremony(awards_card, args)
    else:
        _sideline("The envelopes are sealed and waiting for the award reveal.", "🎬", "yellow")

    return 0


@_with_showtime_pacing()
def cmd_award(args: argparse.Namespace, _gateway: Optional[Any] = None,
              clock: Optional[Callable] = None) -> int:
    """award — declare winners; write award cards; append registry entry."""
    run_id = args.run_id
    winner_id = args.winner
    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)

    _assert_bundle_exists(bundle_path, run_id)
    manifest = load_manifest(bundle_path)
    _assert_status_in(manifest, ["sealed", "awarded"], "award")

    # Assert scores are locked (shadow score sealed)
    shadow = load_shadow_score(bundle_path)
    if shadow is None:
        _print_error(7, "ConfigValidationError", "Shadow score not sealed. Run 'judge' first.")
        return 7

    # Check winner card doesn't already exist
    winner_path = bundle_path / "winner" / "card.json"
    if winner_path.exists():
        _print_error(2, "BundleSealError", "Winner card already exists. Run is already awarded.")
        return 2

    # Find winner's builder name
    submissions = _load_submissions(bundle_path)
    winner_sub = next((s for s in submissions if s["submission_id"] == winner_id), None)
    if winner_sub is None:
        _print_error(7, "ConfigValidationError", f"Submission '{winner_id}' not found.")
        return 7

    try:
        tie_resolutions = _parse_tie_resolutions(
            getattr(args, "tie_resolution", None)
        )
        awards_card = _choose_award_winners(
            bundle_path,
            winner_id,
            clock,
            tie_resolutions,
        )
    except ConfigValidationError as exc:
        _print_error(exc.exit_code, type(exc).__name__, str(exc))
        return exc.exit_code
    if not awards_card.get("awards"):
        _print_error(
            7,
            "ConfigValidationError",
            "No configured awards apply to the available submissions. "
            "Adjust custom award ranks or add eligible submissions.",
        )
        return 7
    declared_at = awards_card["declared_at"]
    grand_prize_name = _event_grand_prize_name(bundle_path)
    grand_prize = next(
        (
            award for award in awards_card["awards"]
            if award.get("award_name") == grand_prize_name
        ),
        awards_card["awards"][0],
    )
    grand_prize_recipients = [
        award["winner_submission_id"]
        for award in awards_card["awards"]
        if award.get("award_name") == grand_prize_name
    ]
    if winner_id not in grand_prize_recipients:
        _print_error(
            7,
            "ConfigValidationError",
            f"Submission '{winner_id}' is not a recipient of {grand_prize_name} "
            "under the sealed tie policy.",
        )
        return 7
    feedback = {
        card.get("submission_id"): card for card in _load_feedback(bundle_path)
    }
    winner_feedback = feedback.get(winner_id, {})
    shared_winner_projects = [
        submission.get("project_name", submission.get("submission_id", "Unknown"))
        for submission in submissions
        if submission.get("submission_id") in grand_prize_recipients
    ]
    winner_card = {
        "run_id": run_id,
        "winner_submission_id": winner_id,
        "winner_submission_ids": grand_prize_recipients,
        "project_name": winner_sub.get("project_name", "Unknown"),
        "winner_builder_name": winner_sub.get("builder_name", "Unknown"),
        "award_name": grand_prize_name,
        "shared_placement": len(grand_prize_recipients) > 1,
        "shared_project_names": shared_winner_projects,
        "declared_at": declared_at,
        "requires_human_approval": True,
        "published": False,
        "awards": awards_card["awards"],
        "why_selected": grand_prize.get("reason", ""),
        "next_commit": winner_feedback.get("next_commit", ""),
    }

    # Tone check winner card text
    winner_verb = "shares" if len(grand_prize_recipients) > 1 else "wins"
    card_text = (
        f"{winner_sub.get('builder_name', '')} {winner_verb} the {grand_prize_name} "
        f"for project {winner_sub.get('project_name', '')}."
    )
    tone = check_tone(card_text, load_rubric(bundle_path), "winner_card", clock)
    assert_tone(tone, "winner card")

    write_once_json(winner_path, winner_card)
    write_once_json(bundle_path / "winner" / "awards.json", awards_card)
    _write_awards_markdown(bundle_path, awards_card)
    winner_md = (
        f"# 🏆 {grand_prize_name}\n\n"
        f"**Project:** {winner_sub.get('project_name', 'Unknown')}  \n"
        f"**Built by:** {winner_sub.get('builder_name', 'Unknown')}  \n"
        f"**Run:** `{run_id}`  \n\n"
        "## Why it stood out\n"
        f"{grand_prize.get('reason', 'This project stood out across the event rubric.')}\n\n"
        "## Next commit nudge\n"
        f"{winner_feedback.get('next_commit', 'Consider extending the strongest part of the project for its next audience.')}\n\n"
        "> Generated by Hackathon Judge. Human approval is required before external publishing.\n"
    )
    if len(shared_winner_projects) > 1:
        winner_md = (
            winner_md.replace(
                "## Why it stood out\n",
                "## Shared placement\n"
                f"This podium placement is shared by: {', '.join(shared_winner_projects)}.\n\n"
                "## Why it stood out\n",
            )
        )
    write_once(bundle_path / "winner" / "card.md", winner_md)

    # Append registry entry
    registry_path = get_registry_path()
    registry_entry = {
        "run_id": run_id,
        "winner_id": winner_id,
        "winner_ids": grand_prize_recipients,
        "award_name": grand_prize_name,
        "declared_at": declared_at,
        "bundle_sha256": "",  # populated after export
    }
    append_ndjson(registry_path, registry_entry)

    # Also append to run-local registry
    local_registry = bundle_path / "registry" / "log.ndjson"
    append_ndjson(local_registry, registry_entry)

    update_status(bundle_path, "awarded", clock)
    log_command(
        bundle_path,
        "award",
        "ok",
        (
            f"winner={winner_id} tie_policy="
            f"{awards_card['tie_ceremony']['policy']['mode']}"
        ),
        clock,
    )

    if not getattr(args, "quiet", False):
        _print_award_ceremony(awards_card, args)
    if not _showtime_enabled(args) and not getattr(args, "quiet", False):
        _warning("Winner card requires human approval before external publishing.")
    return 0


def cmd_recap(args: argparse.Namespace, _gateway: Optional[Any] = None,
              clock: Optional[Callable] = None) -> int:
    """recap — write a workshop recap Markdown file from stored artifacts only."""
    run_id = args.run_id
    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)
    _assert_bundle_exists(bundle_path, run_id)
    manifest = load_manifest(bundle_path)
    _assert_status_in(manifest, ["sealed", "awarded", "exported"], "recap")

    verdicts = _load_verdicts(bundle_path)
    feedback = {f.get("submission_id"): f for f in _load_feedback(bundle_path)}
    winner = None
    winner_path = bundle_path / "winner" / "card.json"
    if winner_path.exists():
        winner = load_json(winner_path)
    awards_card = _load_awards(bundle_path)
    event_name = _event_name(bundle_path)
    scores_revealed = manifest.get("status") in {"awarded", "exported"}

    lines = [
        f"# {event_name} Recap — {run_id}",
        "",
        f"**Mode:** {manifest.get('mode', 'workshop')}",
        f"**Status:** {manifest.get('status', 'unknown')}",
        "",
    ]
    if awards_card:
        lines += ["## Awards", ""]
        for award in awards_card.get("awards", []):
            lines += [
                f"### {award.get('emoji', '🏆')} {award.get('award_name', 'Award')}",
                "",
                f"**Project:** {award.get('project_name', 'Unknown')}  ",
                f"**Built by:** {award.get('winner_builder_name', 'Unknown')}  ",
                "",
                award.get("tagline", ""),
                "",
                f"**Why it stood out:** {award.get('reason', '')}",
                "",
            ]
    elif winner:
        lines += [
            f"## 🏆 {winner.get('award_name', _event_grand_prize_name(bundle_path))}",
            "",
            f"**Winner:** {winner.get('winner_builder_name', 'Unknown')}",
            "",
        ]
    lines += ["## Project Spotlights", ""]
    for v in verdicts:
        sid = v.get("submission_id")
        fb = feedback.get(sid, {})
        lines += [
            f"### {v.get('project_name', sid)}",
            "",
            f"- **Builder:** {v.get('builder_name', 'Unknown')}",
            f"- **Bright spot:** {fb.get('bright_spot', 'This build showed real promise.')}",
            f"- **Next commit nudge:** {fb.get('next_commit', 'Consider adding a quick-start path for the next user.')}",
            "",
        ]
        if scores_revealed:
            lines.insert(
                len(lines) - 1,
                f"- **Score:** {float(v.get('total_score', 0)):.2f}/10",
            )
    lines += [
        "---",
        "Generated from stored artifacts only. No live model calls.",
        "",
    ]
    recap_path = Path(getattr(args, "out", "") or (bundle_path / "recap.md"))
    recap_path.parent.mkdir(parents=True, exist_ok=True)
    recap_path.write_text("\n".join(lines), encoding="utf-8")
    _success(f"Workshop recap written: {recap_path}")
    return 0


def cmd_tui(args: argparse.Namespace, _gateway: Optional[Any] = None,
            clock: Optional[Callable] = None) -> int:
    """tui — live Textual dashboard; projector mode requires a real TTY."""
    run_id = getattr(args, "run_id", None)
    projector = getattr(args, "projector", False)
    operator = getattr(args, "operator", False)

    if projector and not run_id:
        _print_error(
            7,
            "ConfigValidationError",
            "Optional monitor mode requires a run ID.",
        )
        return 7
    if projector and not sys.stdout.isatty():
        _print_error(
            7,
            "ConfigValidationError",
            "The optional monitor requires a real interactive terminal; "
            "open a terminal window and rerun this command.",
        )
        return 7

    # Try launching the Textual dashboard
    if run_id and sys.stdout.isatty():
        textual_ready, textual_detail = _textual_status()
        if not textual_ready:
            if projector:
                _print_error(
                    7,
                    "ConfigValidationError",
                    f"{textual_detail}; "
                    f"run: {shlex.join([sys.executable, '-m', 'pip', 'install', 'textual>=8,<9'])}",
                )
                return 7
            _warning(f"{textual_detail}; falling back to CLI presenter.")
        else:
            try:
                from hackathon_judge_dashboard import BuilderDashboard
                app = BuilderDashboard(run_id=run_id, projector=projector, operator=operator)
                app.run()
                return 0
            except Exception as exc:
                if projector:
                    _print_error(
                        7,
                        "ConfigValidationError",
                        f"Optional monitor failed to start: {exc}",
                    )
                    return 7
                _warning(f"Dashboard error ({exc}); falling back to CLI presenter.")

    # Graceful CLI fallback
    if run_id:
        _magic_banner("Hackathon Judge Optional Monitor", "Artifact-powered run status")
        return cmd_present(args, _gateway, clock)

    _magic_banner("Hackathon Judge Optional Monitor", "Choose a sealed run to inspect")
    return cmd_list(args, _gateway, clock)


def _validate_archive_snapshot(
    archive_path: Path,
    run_id: str,
    bundle_path: Path,
) -> tuple[bool, str]:
    """Verify that an existing archive exactly matches the live sealed bundle."""
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            file_members: Dict[str, tarfile.TarInfo] = {}
            seen_names = set()
            for member in archive.getmembers():
                if member.name in seen_names:
                    return False, f"archive contains a duplicate entry: {member.name}"
                seen_names.add(member.name)
                member_path = PurePosixPath(member.name)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or not member_path.parts
                    or member_path.parts[0] != run_id
                ):
                    return False, f"archive contains an invalid path: {member.name}"
                if not (member.isdir() or member.isfile()):
                    return False, f"archive contains an unsupported entry: {member.name}"
                if member.isfile():
                    file_members[member.name] = member

            live_files = [
                *collect_bundle_artifacts(bundle_path),
                bundle_path / "HASHES",
                bundle_path / "SEAL",
            ]
            expected_files = {
                f"{run_id}/{path.relative_to(bundle_path).as_posix()}": path
                for path in live_files
            }
            for member_name, live_path in expected_files.items():
                member = file_members.get(member_name)
                if member is None:
                    return False, f"archive is missing artifact: {member_name}"
                artifact_file = archive.extractfile(member)
                if artifact_file is None:
                    return False, f"archive artifact could not be read: {member_name}"
                if artifact_file.read() != live_path.read_bytes():
                    return False, f"archive artifact differs from live bundle: {member_name}"

            unexpected = set(file_members) - set(expected_files)
            if unexpected:
                return False, f"archive contains unexpected files: {sorted(unexpected)[0]}"
    except (OSError, tarfile.TarError, ValueError) as exc:
        return False, str(exc)
    return True, ""


def cmd_export(args: argparse.Namespace, _gateway: Optional[Any] = None,
               clock: Optional[Callable] = None) -> int:
    """export — package full immutable bundle; write HASHES + SEAL + tar.gz."""
    run_id = args.run_id
    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)
    force = getattr(args, "force", False)

    _assert_bundle_exists(bundle_path, run_id)
    manifest = load_manifest(bundle_path)
    _assert_status_in(manifest, ["sealed", "awarded", "exported"], "export")

    # Archives remain internal artifacts. Approval is enforced by any external
    # publishing workflow, and the card's approval state travels in the bundle.
    winner_path = bundle_path / "winner" / "card.json"
    if winner_path.exists():
        winner = load_json(winner_path)
        if winner.get("requires_human_approval", True) and not winner.get("published", False):
            _warning("Winner material remains internal until a human approves external publishing.")

    # A sealed bundle is write-once. Re-sealing would replace the evidence that
    # makes replay and validation meaningful, so --force is intentionally refused.
    seal_path = bundle_path / "SEAL"
    hashes_path = bundle_path / "HASHES"
    archive_name = f"{run_id}.bundle.tar.gz"
    archive_path = runs_dir / archive_name
    if force:
        _print_error(2, "BundleSealError", "Re-sealing an existing bundle is not supported.")
        return 2
    if hashes_path.exists() and not seal_path.exists():
        resumed, detail = _resume_partial_seal(bundle_path)
        if not resumed:
            _print_error(
                5,
                "BundleTamperError",
                f"HASHES exists without SEAL and could not be resumed safely: {detail}",
            )
            return 5
        print(f"  [1/3] Partial SEAL resumed: {detail[:16]}...")
    if seal_path.exists():
        if archive_path.exists():
            validation_stdout = io.StringIO()
            validation_stderr = io.StringIO()
            with contextlib.redirect_stdout(validation_stdout), contextlib.redirect_stderr(validation_stderr):
                rc = cmd_validate(
                    argparse.Namespace(bundle=str(bundle_path.resolve())),
                    _gateway,
                    clock,
                )
            if rc:
                print(validation_stdout.getvalue(), end="")
                print(validation_stderr.getvalue(), end="", file=sys.stderr)
                return rc
            archive_valid, archive_detail = _validate_archive_snapshot(
                archive_path,
                run_id,
                bundle_path,
            )
            if not archive_valid:
                _print_error(
                    5,
                    "BundleTamperError",
                    f"Existing archive is invalid: {archive_detail}",
                )
                return 5
            archive_sha = _sha256_file(archive_path)
            print(f"✓ Bundle already exported and valid: {archive_path}")
            print(f"  SHA-256: {archive_sha}")
            return 0
        if not hashes_path.exists():
            _print_error(
                5,
                "BundleTamperError",
                "SEAL exists without HASHES; the partial export cannot be resumed safely.",
            )
            return 5
        validation_stdout = io.StringIO()
        validation_stderr = io.StringIO()
        with contextlib.redirect_stdout(validation_stdout), contextlib.redirect_stderr(validation_stderr):
            rc = cmd_validate(
                argparse.Namespace(bundle=str(bundle_path.resolve())),
                _gateway,
                clock,
            )
        if rc:
            print(validation_stdout.getvalue(), end="")
            print(validation_stderr.getvalue(), end="", file=sys.stderr)
            return rc
        print("  [1/3] Existing SEAL verified; resuming archive creation...")
    else:
        if archive_path.exists():
            _print_error(
                2,
                "BundleSealError",
                f"Archive already exists without a SEAL: {archive_path}",
            )
            return 2

        # Update manifest status and log BEFORE computing HASHES so the final state is captured.
        update_status(bundle_path, "exported", clock)
        log_command(bundle_path, "export", "ok", "sealing", clock)

        print("  [1/3] Hashing artifacts...")
        try:
            _, seal_hash = write_hashes_and_seal(bundle_path)
        except (BundleSealError, OSError) as exc:
            _print_error(1, "SealWriteError", f"Could not write bundle seal: {exc}")
            return 1
        print(f"  [1/3] SEAL: {seal_hash[:16]}...")

    print("  [2/3] Creating bundle archive...")
    temporary_archive = runs_dir / f".{archive_name}.{uuid.uuid4().hex}.tmp"
    try:
        with tarfile.open(temporary_archive, "w:gz") as tar:
            tar.add(bundle_path, arcname=run_id)
        os.replace(temporary_archive, archive_path)
    except (OSError, tarfile.TarError) as exc:
        temporary_archive.unlink(missing_ok=True)
        _print_error(1, "ArchiveError", f"Could not create bundle archive: {exc}")
        return 1

    archive_sha = _sha256_file(archive_path)
    print(f"  [2/3] Archive: {archive_path} (SHA-256: {archive_sha[:16]}...)")

    print(f"\n✓ Bundle exported: {archive_path}")
    print(f"  SHA-256: {archive_sha}")
    return 0


def cmd_validate(args: argparse.Namespace, _gateway: Optional[Any] = None,
                 clock: Optional[Callable] = None) -> int:
    """validate — verify bundle HASHES and SEAL integrity."""
    bundle_arg = getattr(args, "bundle", None) or getattr(args, "run_id", None)
    runs_dir = get_runs_dir()

    # Determine bundle path (may be a run_id or a direct path)
    if bundle_arg and Path(bundle_arg).is_dir():
        bundle_path = Path(bundle_arg)
    elif bundle_arg:
        bundle_path = get_bundle_path(bundle_arg, runs_dir)
    else:
        _print_error(7, "ConfigValidationError", "Provide --bundle <path> or a run_id.")
        return 7

    if not bundle_path.exists():
        _print_error(7, "ConfigValidationError", f"Bundle not found: {bundle_path}")
        return 7

    seal_path = bundle_path / "SEAL"
    hashes_path = bundle_path / "HASHES"

    if not seal_path.exists() or not hashes_path.exists():
        _print_error(5, "BundleTamperError",
                     "SEAL or HASHES missing — bundle not exported or may be tampered.")
        return 5

    print(f"Validating bundle: {bundle_path}")

    # Step 1: Read stored SEAL
    stored_seal = seal_path.read_text(encoding="utf-8").strip()

    # Step 2: Recompute SHA-256 of HASHES content
    hashes_content = hashes_path.read_text(encoding="utf-8")
    recomputed_seal = _sha256_bytes(hashes_content.encode("utf-8"))

    if stored_seal != recomputed_seal:
        _print_error(5, "BundleTamperError",
                     f"SEAL mismatch! Stored={stored_seal[:16]}... Computed={recomputed_seal[:16]}...")
        return 5
    print(f"  ✓ SEAL integrity: OK ({stored_seal[:16]}...)")

    # Step 3: Verify each artifact hash
    failures = []
    stored_paths = set()
    for line in hashes_content.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2:
            failures.append(f"INVALID HASH LINE: {line}")
            continue
        stored_hash, rel_path = parts
        if rel_path in stored_paths:
            failures.append(f"DUPLICATE HASH PATH: {rel_path}")
            continue
        stored_paths.add(rel_path)
        artifact_path = (bundle_path / rel_path).resolve()
        try:
            artifact_path.relative_to(bundle_path.resolve())
        except ValueError:
            failures.append(f"INVALID PATH: {rel_path}")
            continue
        if not artifact_path.exists():
            failures.append(f"MISSING: {rel_path}")
            continue
        actual_hash = _sha256_file(artifact_path)
        if actual_hash != stored_hash:
            failures.append(f"TAMPERED: {rel_path} (expected {stored_hash[:16]}... got {actual_hash[:16]}...)")

    actual_paths = {
        str(path.relative_to(bundle_path))
        for path in collect_bundle_artifacts(bundle_path)
    }
    for unexpected_path in sorted(actual_paths - stored_paths):
        failures.append(f"UNSEALED ARTIFACT: {unexpected_path}")

    if failures:
        for f in failures:
            print(f"  ✗ {f}", file=sys.stderr)
        _print_error(5, "BundleTamperError", f"{len(failures)} artifact(s) failed hash check.")
        return 5

    # Step 4: Assert shadow score hash matches
    ss_path = bundle_path / "sealed" / "shadow_score.json"
    if ss_path.exists():
        print(f"  ✓ Shadow score: present (hash verified)")
    else:
        print(f"  ⚠ Shadow score: not present (run may not have been judged)")

    artifact_count = len(stored_paths)
    print(f"\n✓ Validation PASSED — {artifact_count} artifact(s) verified.")
    return 0


def cmd_list(args: argparse.Namespace, _gateway: Optional[Any] = None,
             clock: Optional[Callable] = None) -> int:
    """list — enumerate all runs and statuses."""
    runs_dir = get_runs_dir()

    if not runs_dir.exists():
        print("No runs found.")
        return 0

    runs = []
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        manifest_path = run_dir / "manifest" / "bundle.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = load_json(manifest_path)
            subs = len(list((run_dir / "inputs").glob("*.json"))) if (run_dir / "inputs").exists() else 0
            runs.append({
                "run_id": manifest.get("run_id", run_dir.name),
                "status": manifest.get("status", "unknown"),
                "mode": manifest.get("mode", "workshop"),
                "created_at": manifest.get("created_at", ""),
                "submissions": subs,
            })
        except Exception:
            continue

    if not runs:
        print("No runs found.")
        return 0

    _magic_banner("Hackathon Judge Runs", "Every run is replayable. Every bundle is proof.")
    print(_paint(f"{'RUN ID':<40} {'STATUS':<12} {'MODE':<10} {'SUBS':>4}  CREATED", "cyan", bold=True))
    print(_paint("-" * 90, "blue"))
    for r in runs:
        created = r["created_at"][:19] if r["created_at"] else ""
        status_icon = "🏆" if r["status"] == "awarded" else "🔒" if r["status"] in ("sealed", "exported") else "🧪"
        print(f"{status_icon} {r['run_id']:<38} {r['status']:<12} {r['mode']:<10} {r['submissions']:>4}  {created}")
    return 0


def _safe_extract_tar(archive: tarfile.TarFile, destination: Path) -> None:
    """Extract only regular files and directories contained by destination."""
    destination = destination.resolve()
    members = archive.getmembers()
    for member in members:
        target = (destination / member.name).resolve()
        try:
            target.relative_to(destination)
        except ValueError as exc:
            raise ConfigValidationError(
                f"Replay archive contains a path outside its extraction directory: {member.name}"
            ) from exc
        if not (member.isdir() or member.isfile()):
            raise ConfigValidationError(
                f"Replay archive contains an unsupported link or device entry: {member.name}"
            )
    for member in members:
        archive.extract(member, path=destination)


def cmd_replay(args: argparse.Namespace, _gateway: Optional[Any] = None,
               clock: Optional[Callable] = None) -> int:
    """replay — read-only re-run of any prior bundle; no model calls, no new artifacts."""
    bundle_arg = getattr(args, "bundle", None) or getattr(args, "run_id", None)
    runs_dir = get_runs_dir()

    if bundle_arg and Path(bundle_arg).is_dir():
        bundle_path = Path(bundle_arg)
    elif bundle_arg:
        # Could be a .tar.gz bundle
        archive = Path(bundle_arg) if Path(bundle_arg).exists() else get_bundle_path(bundle_arg, runs_dir)
        if archive.suffix == ".gz" and archive.exists():
            # Extract to temp location in runs dir and replay
            extract_dir = runs_dir / f"_replay_{uuid.uuid4().hex[:8]}"
            extract_dir.mkdir(parents=True)
            try:
                with tarfile.open(archive, "r:gz") as tar:
                    _safe_extract_tar(tar, extract_dir)
            except (tarfile.TarError, ConfigValidationError) as exc:
                shutil.rmtree(extract_dir, ignore_errors=True)
                _print_error(7, "ConfigValidationError", f"Could not safely extract replay archive: {exc}")
                return 7
            # Find the run dir inside
            subdirs = [d for d in extract_dir.iterdir() if d.is_dir()]
            if subdirs:
                bundle_path = subdirs[0]
            else:
                bundle_path = extract_dir
        else:
            bundle_path = get_bundle_path(bundle_arg, runs_dir)
    else:
        _print_error(7, "ConfigValidationError", "Provide --bundle <path> or a run_id.")
        return 7

    if not bundle_path.exists():
        _print_error(7, "ConfigValidationError", f"Bundle not found: {bundle_path}")
        return 7

    # Validate first
    seal_path = bundle_path / "SEAL"
    hashes_path = bundle_path / "HASHES"
    if seal_path.exists() and hashes_path.exists():
        _sideline("Validating bundle integrity before replay...", "📼", "blue")
        # Quick seal check
        stored_seal = seal_path.read_text(encoding="utf-8").strip()
        hashes_content = hashes_path.read_text(encoding="utf-8")
        recomputed = _sha256_bytes(hashes_content.encode("utf-8"))
        if stored_seal != recomputed:
            _print_error(5, "BundleTamperError", "Bundle integrity check failed — cannot replay tampered bundle.")
            return 5
        _success("Bundle integrity: OK")

    # Read all artifacts and render (read-only, no model calls, no new files)
    manifest = load_manifest(bundle_path)
    rubric = load_rubric(bundle_path)
    verdicts = _load_verdicts(bundle_path)
    feedback = _load_feedback(bundle_path)
    gate = None
    gate_path = bundle_path / "freshness_gate.json"
    if gate_path.exists():
        gate = load_json(gate_path)

    event_name = _event_name(bundle_path)
    scores_revealed = manifest.get("status") in {"awarded", "exported"}
    _magic_banner(f"{event_name} Replay", f"Run: {manifest.get('run_id', bundle_path.name)}")
    _sideline(f"Status: {manifest.get('status', 'unknown')}", "📼", "blue")
    if gate:
        _sideline(
            f"Panel: {_model_panel_label(gate)} ({gate.get('status', '')})",
            "🧠",
            "green",
        )

    if verdicts:
        print(_paint("\n🎙️ Panel Verdicts", "magenta", bold=True))
        for v in verdicts:
            score = float(v.get("total_score", 0))
            print(_paint(f"\n  ─── 🛠️ {v.get('project_name', v['submission_id'])} ───", "blue", bold=True))
            print(_paint(f"  Builder: {v.get('builder_name', '')}", "cyan"))
            if scores_revealed:
                print(_paint(f"  Score:   {score:.2f}/10  {_score_bar(score)}", "gold", bold=True))
            else:
                print(_paint("  Score:   sealed until the award reveal", "gold", bold=True))
            for arch_v in v.get("archetype_verdicts", []):
                reaction = arch_v.get("bright_spot", "")
                if not scores_revealed:
                    reaction = _audience_safe_commentary(
                        reaction,
                        "The panel found a thoughtful detail worth celebrating.",
                    )
                print(_paint(f"    🎙️ {arch_v['archetype_name']}: {reaction[:100]}", "green"))
    else:
        print("\n  No verdicts found in bundle.")

    if feedback:
        print(_paint("\n✨ Next-Commit Nudges", "cyan", bold=True))
        for fc in feedback:
            print(_paint(f"\n  Builder: {fc.get('builder_name', fc['submission_id'])}", "cyan"))
            bright_spot = fc.get("bright_spot", "")
            next_commit = fc.get("next_commit", "")
            if not scores_revealed:
                bright_spot = _audience_safe_commentary(
                    bright_spot,
                    "This project brought a thoughtful moment to the room.",
                )
                next_commit = _audience_safe_commentary(
                    next_commit,
                    "A helpful next step will be shared after the reveal.",
                )
            print(_paint(f"  ✨ {bright_spot}", "green"))
            print(_paint(f"  ➜ {next_commit}", "yellow"))

    winner_path = bundle_path / "winner" / "card.json"
    awards_card = _load_awards(bundle_path)
    if scores_revealed and awards_card:
        print()
        _print_award_ceremony(awards_card, args)
    elif scores_revealed and winner_path.exists():
        winner = load_json(winner_path)
        print()
        _magic_banner(
            f"🏆 {winner.get('award_name', _event_grand_prize_name(bundle_path))}",
            f"{winner.get('winner_builder_name', 'Unknown')}",
        )

    return 0


def cmd_resume(args: argparse.Namespace, _gateway: Optional[Any] = None,
               clock: Optional[Callable] = None) -> int:
    """resume — re-enter an interrupted judge run at the last completed step."""
    run_id = args.run_id
    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)

    _assert_bundle_exists(bundle_path, run_id)
    manifest = load_manifest(bundle_path)

    if manifest.get("status") == "judging":
        print(f"[INFO] Resuming interrupted judge run for '{run_id}'...")
        args_copy = argparse.Namespace(**vars(args))
        args_copy.run_id = run_id
        return cmd_judge(args_copy, _gateway, clock)
    elif manifest.get("status") in ("sealed", "awarded", "exported"):
        print(f"[INFO] Run '{run_id}' is already complete (status: {manifest['status']}).")
        return 0
    else:
        print(f"[INFO] Run '{run_id}' is in status '{manifest['status']}'. Nothing to resume.")
        return 0


def cmd_compare(args: argparse.Namespace, _gateway: Optional[Any] = None,
                clock: Optional[Callable] = None) -> int:
    """compare — side-by-side diff of two sealed run bundles."""
    runs_dir = get_runs_dir()
    bundle_a_arg = args.bundle_a
    bundle_b_arg = args.bundle_b

    def _resolve(arg: str) -> Path:
        p = Path(arg)
        if p.is_dir():
            return p
        return get_bundle_path(arg, runs_dir)

    bundle_a = _resolve(bundle_a_arg)
    bundle_b = _resolve(bundle_b_arg)

    for b in (bundle_a, bundle_b):
        if not b.exists():
            _print_error(7, "ConfigValidationError", f"Bundle not found: {b}")
            return 7

    manifest_a = load_manifest(bundle_a)
    manifest_b = load_manifest(bundle_b)
    verdicts_a = {v["submission_id"]: v for v in _load_verdicts(bundle_a)}
    verdicts_b = {v["submission_id"]: v for v in _load_verdicts(bundle_b)}

    print("=" * 70)
    print(f"  COMPARE")
    print(f"  A: {manifest_a.get('run_id', bundle_a.name)} (status: {manifest_a.get('status', '?')})")
    print(f"  B: {manifest_b.get('run_id', bundle_b.name)} (status: {manifest_b.get('status', '?')})")
    print("=" * 70)

    # Shadow scores (only if awarded/exported — per visibility rule)
    ss_a = load_shadow_score(bundle_a)
    ss_b = load_shadow_score(bundle_b)

    if ss_a and ss_b and (manifest_a.get("status") in ("awarded", "exported")) \
            and (manifest_b.get("status") in ("awarded", "exported")):
        print("\n  Score Comparison:")
        all_sids = sorted(set(list(ss_a["scores"].keys()) + list(ss_b["scores"].keys())))
        print(f"  {'Submission':<38} {'Score A':>8} {'Score B':>8}  {'Δ':>6}")
        print("  " + "-" * 62)
        for sid in all_sids:
            sa = ss_a["scores"].get(sid, "—")
            sb = ss_b["scores"].get(sid, "—")
            delta = ""
            if isinstance(sa, float) and isinstance(sb, float):
                delta = f"{sb - sa:+.2f}"
            print(f"  {sid:<38} {str(sa):>8} {str(sb):>8}  {delta:>6}")

    print("\n  Structure Diff:")
    dirs = ["manifest", "config", "inputs", "eval", "sealed", "verdicts", "feedback", "winner", "registry"]
    for d in dirs:
        count_a = len(list((bundle_a / d).rglob("*"))) if (bundle_a / d).exists() else 0
        count_b = len(list((bundle_b / d).rglob("*"))) if (bundle_b / d).exists() else 0
        indicator = "=" if count_a == count_b else "≠"
        print(f"  {indicator} {d:<20} A:{count_a:>3}  B:{count_b:>3}")

    return 0


def _awards_by_submission(awards_card: Optional[Dict]) -> Dict[str, List[Dict]]:
    """Index declared awards without exposing a pre-award partial artifact."""
    by_submission: Dict[str, List[Dict]] = {}
    for award in (awards_card or {}).get("awards", []):
        if not isinstance(award, dict):
            continue
        submission_id = award.get("winner_submission_id")
        if submission_id:
            by_submission.setdefault(str(submission_id), []).append(award)
    return by_submission


def _fallback_judge_highlights(verdict: Dict) -> List[Dict]:
    """Recover useful context for a legacy or partially written feedback card."""
    highlights: List[Dict] = []
    for reaction in verdict.get("archetype_verdicts", []):
        if not isinstance(reaction, dict):
            continue
        text = _compact_text(
            reaction.get("bright_spot") or reaction.get("perspective")
        )
        if text:
            highlights.append({
                "lens_id": str(reaction.get("archetype_id") or ""),
                "lens": str(reaction.get("archetype_name") or "Panel lens"),
                "highlight": text,
            })
    return highlights


def _project_feedback_proposal(
    submission: Dict,
    verdict: Dict,
    feedback: Dict,
    awards: List[Dict],
    clock: Optional[Callable] = None,
) -> Dict:
    """Build one human-reviewable report entirely from sealed run artifacts."""
    judges_liked = feedback.get("judges_liked")
    if not isinstance(judges_liked, list):
        judges_liked = _fallback_judge_highlights(verdict)

    selected_for = [
        {
            "award_id": award.get("award_id", ""),
            "award_name": award.get("award_name", "Award"),
            "why_selected": award.get(
                "reason",
                "This project stood out through a strong response to the event rubric.",
            ),
        }
        for award in awards
    ]
    return {
        "submission_id": submission.get("submission_id", ""),
        "builder_name": submission.get("builder_name", ""),
        "project_name": submission.get("project_name", ""),
        "selected_for": selected_for,
        "judges_liked": judges_liked,
        "bright_spot": feedback.get(
            "bright_spot",
            "The panel recorded a thoughtful strength in this submission.",
        ),
        "ways_to_improve": feedback.get(
            "next_commit",
            "Consider choosing one concrete next step that expands the project's strongest idea.",
        ),
        "next_commit": feedback.get(
            "next_commit",
            "Consider choosing one concrete next step that expands the project's strongest idea.",
        ),
        "extended_guidance": feedback.get(
            "panel_notes",
            "The panel's stored feedback is ready for a human reviewer.",
        ),
        "copilot_use": feedback.get(
            "copilot_use",
            _submitted_evidence_assessment(submission, "copilot_evidence", "Copilot use"),
        ),
        "innovation_signal": feedback.get(
            "innovation_signal",
            _innovation_signal(judges_liked),
        ),
        "frontier_use": feedback.get(
            "frontier_use",
            _submitted_evidence_assessment(submission, "frontier_evidence", "frontier use"),
        ),
        "copilot_next_moves": feedback.get(
            "copilot_next_moves",
            [
                (
                    "Use Copilot to turn the project's primary user journey into a "
                    "small implementation plan and acceptance-test checklist."
                )
            ],
        ),
        "frontier_experiments": feedback.get(
            "frontier_experiments",
            [
                (
                    "Prototype a focused, human-reviewed agent workflow using only "
                    "project-approved context before broadening the experience."
                )
            ],
        ),
        "requires_human_approval": True,
        "generated_at": _now(clock),
    }


def _build_feedback_proposals(
    bundle_path: Path,
    submission_id: Optional[str] = None,
    clock: Optional[Callable] = None,
) -> List[Dict]:
    """Create durable-feedback reports without calling a model again."""
    submissions = _load_submissions(bundle_path)
    if submission_id:
        submissions = [sub for sub in submissions if sub["submission_id"] == submission_id]
        if not submissions:
            raise ConfigValidationError(f"Submission '{submission_id}' not found.")

    verdicts = {verdict.get("submission_id"): verdict for verdict in _load_verdicts(bundle_path)}
    feedback = {card.get("submission_id"): card for card in _load_feedback(bundle_path)}
    manifest = load_manifest(bundle_path)
    awards_card = _load_awards(bundle_path) if manifest.get("status") in {"awarded", "exported"} else None
    awards = _awards_by_submission(awards_card)

    return [
        _project_feedback_proposal(
            submission,
            verdicts.get(submission["submission_id"], {}),
            feedback.get(submission["submission_id"], {}),
            awards.get(submission["submission_id"], []),
            clock,
        )
        for submission in submissions
    ]


def _write_feedback_proposals(
    runs_dir: Path,
    run_id: str,
    proposals: List[Dict],
) -> Path:
    """Write a reviewable proposal outside the immutable result bundle."""
    proposal_dir = runs_dir.parent / "feedback_proposals" / run_id
    proposal_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    proposal_path = proposal_dir / f"proposal_{timestamp}.json"
    proposal_path.write_text(
        json.dumps({"proposals": proposals}, indent=2),
        encoding="utf-8",
    )
    return proposal_path


def _print_feedback_proposals(proposals: List[Dict], proposal_path: Path) -> None:
    """Print an operator-only summary that stays clear of numeric score language."""
    print(f"Feedback proposal written: {proposal_path}")
    print("Human approval is required before delivery.")
    for proposal in proposals:
        print(f"\nProject: {proposal['project_name']} ({proposal['builder_name']})")
        for selection in proposal["selected_for"]:
            print(f"Selected for {selection['award_name']}: {selection['why_selected']}")
        print("Judges liked:")
        for highlight in proposal["judges_liked"]:
            if isinstance(highlight, dict):
                print(f"- {highlight.get('lens', 'Panel lens')}: {highlight.get('highlight', '')}")
        print(f"Copilot use: {proposal['copilot_use'].get('summary', '')}")
        print(f"Innovation: {proposal['innovation_signal'].get('summary', '')}")
        print(f"Frontier use: {proposal['frontier_use'].get('summary', '')}")
        print(f"Next step: {proposal['ways_to_improve']}")
        print("Copilot next move:")
        for suggestion in proposal["copilot_next_moves"]:
            print(f"- {suggestion}")
        print("Frontier experiment:")
        for experiment in proposal["frontier_experiments"]:
            print(f"- {experiment}")


def cmd_feedback(args: argparse.Namespace, _gateway: Optional[Any] = None,
                 clock: Optional[Callable] = None) -> int:
    """
    feedback — produce a human-readable proposal from stored judging artifacts.
    Does NOT modify any existing bundle artifact or call a model again.
    """
    run_id = args.run_id
    submission_id = getattr(args, "submission_id", None)
    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)

    _assert_bundle_exists(bundle_path, run_id)
    manifest = load_manifest(bundle_path)
    _assert_status_in(manifest, ["sealed", "awarded", "exported"], "feedback")

    try:
        proposals = _build_feedback_proposals(bundle_path, submission_id, clock)
    except ConfigValidationError as exc:
        _print_error(exc.exit_code, type(exc).__name__, str(exc))
        return exc.exit_code

    proposal_path = _write_feedback_proposals(runs_dir, run_id, proposals)
    if not getattr(args, "quiet", False):
        _print_feedback_proposals(proposals, proposal_path)
    return 0


def cmd_doctor(args: argparse.Namespace, _gateway: Optional[Any] = None,
               clock: Optional[Callable] = None) -> int:
    """doctor — diagnose config, model gate, and bundle health without modifying state."""
    run_id = getattr(args, "run_id", None)
    runs_dir = get_runs_dir()
    issues: List[str] = []
    ok: List[str] = []

    print("Hackathon Judge — Doctor")
    print("=" * 50)

    # 1. Check Python version
    vi = sys.version_info
    if vi >= (3, 11):
        ok.append(f"Python {vi.major}.{vi.minor}.{vi.micro} (≥ 3.11 ✓)")
    else:
        issues.append(f"Python {vi.major}.{vi.minor} < 3.11 (upgrade required)")

    # 2. Check optional monitor dependency
    textual_ready, textual_detail = _textual_status()
    if textual_ready:
        ok.append(f"Optional monitor: {textual_detail}")
    else:
        ok.append(
            f"Optional monitor unavailable: {textual_detail} "
            f"(run: {shlex.join([sys.executable, '-m', 'pip', 'install', 'textual>=8,<9'])})"
        )

    # 3. Check runs directory
    if runs_dir.exists():
        ok.append(f"Runs directory: {runs_dir}")
    else:
        ok.append(f"Runs directory not yet created: {runs_dir} (will be created on first init)")

    # 4. Check registry
    registry_path = get_registry_path()
    if registry_path.exists():
        entries = read_ndjson(registry_path)
        ok.append(f"Registry: {registry_path} ({len(entries)} entries)")
    else:
        ok.append(f"Registry not yet created: {registry_path} (will be created on first award)")

    # 5. Model gate ping
    try:
        models = query_available_models(_gateway)
        non_deprecated = [m for m in models if not m.get("deprecated", False)]
        ok.append(f"Model gate: {len(models)} models available, {len(non_deprecated)} non-deprecated")
        best = _select_best_model(models)
        ok.append(f"Best available model: {best}")
    except Exception as exc:
        issues.append(f"Model gate: {exc}")

    # 6. Specific bundle check
    if run_id:
        bundle_path = get_bundle_path(run_id, runs_dir)
        if bundle_path.exists():
            manifest = load_manifest(bundle_path)
            status = manifest.get("status", "unknown")
            ok.append(f"Run '{run_id}': status={status}")
            if status == "failed":
                failure_detail = manifest.get("projector_launch_error", "see run artifacts")
                issues.append(f"  Run failed: {failure_detail}")

            # Check required files
            for reqf in ["manifest/bundle.json", "config/rubric.json"]:
                p = bundle_path / reqf
                if p.exists():
                    ok.append(f"  {reqf}: present")
                else:
                    issues.append(f"  {reqf}: MISSING")

            # Check rubric weights
            try:
                rubric = load_rubric(bundle_path)
                _validate_rubric(rubric)
                ok.append(f"  Rubric: valid (weights sum to 1.0)")
            except ConfigValidationError as e:
                issues.append(f"  Rubric: {e}")

            # Check seal integrity if exported
            seal_path = bundle_path / "SEAL"
            hashes_path = bundle_path / "HASHES"
            if seal_path.exists() and hashes_path.exists():
                stored_seal = seal_path.read_text().strip()
                hashes_content = hashes_path.read_text()
                recomputed = _sha256_bytes(hashes_content.encode())
                if stored_seal == recomputed:
                    ok.append(f"  Bundle seal: VALID")
                else:
                    issues.append(f"  Bundle seal: TAMPERED")
            elif seal_path.exists() or hashes_path.exists():
                issues.append("  Bundle seal: INCOMPLETE (SEAL and HASHES must both exist)")
            elif status in ("awarded", "exported"):
                issues.append(
                    f"  Bundle is not export-validated; run 'hackathon-judge export {run_id}'"
                )
            archive_path = runs_dir / f"{run_id}.bundle.tar.gz"
            if status == "exported":
                if archive_path.exists():
                    ok.append(f"  Replay archive: present ({archive_path.name})")
                else:
                    issues.append(
                        f"  Replay archive: MISSING (resume with 'hackathon-judge export {run_id}')"
                    )
        else:
            issues.append(f"Run '{run_id}': not found at {bundle_path}")

    # Report
    for item in ok:
        print(f"  ✓ {item}")
    for item in issues:
        print(f"  ✗ {item}", file=sys.stderr)

    if issues:
        print(f"\n  {len(issues)} issue(s) found.")
        return 1
    else:
        print(f"\n  All checks passed.")
        return 0


# ---------------------------------------------------------------------------
# Layer 7 — Internal helpers
# ---------------------------------------------------------------------------

def _assert_bundle_exists(bundle_path: Path, run_id: str) -> None:
    if not bundle_path.exists():
        _print_error(7, "ConfigValidationError",
                     f"Run '{run_id}' not found at {bundle_path}. Use 'init' first.")
        sys.exit(7)


def _assert_status_in(manifest: Dict, allowed: List[str], command: str) -> None:
    status = manifest.get("status", "unknown")
    if status not in allowed:
        _print_error(7, "ConfigValidationError",
                     f"Command '{command}' requires status in {allowed}, but run is '{status}'.")
        sys.exit(7)


def _load_submissions(bundle_path: Path) -> List[Dict]:
    subs = []
    inputs_dir = bundle_path / "inputs"
    if not inputs_dir.exists():
        return subs
    for p in sorted(inputs_dir.glob("*.json")):
        try:
            subs.append(load_json(p))
        except Exception:
            continue
    return subs


def _load_verdicts(bundle_path: Path) -> List[Dict]:
    verdicts = []
    verdicts_dir = bundle_path / "verdicts"
    if not verdicts_dir.exists():
        return verdicts
    for p in sorted(verdicts_dir.glob("*.json")):
        try:
            verdicts.append(load_json(p))
        except Exception:
            continue
    return verdicts


def _load_feedback(bundle_path: Path) -> List[Dict]:
    cards = []
    fb_dir = bundle_path / "feedback"
    if not fb_dir.exists():
        return cards
    for p in sorted(fb_dir.glob("*.json")):
        try:
            cards.append(load_json(p))
        except Exception:
            continue
    return cards


def _print_error(code: int, cls: str, msg: str) -> None:
    print(f"[ERROR {code}] {cls}: {msg}", file=sys.stderr)


def _hard_error(exc: HackathonJudgeError,
                bundle_path: Optional[Path],
                clock: Optional[Callable] = None) -> None:
    _print_error(exc.exit_code, type(exc).__name__, str(exc))
    if bundle_path and bundle_path.exists():
        try:
            log_command(bundle_path, "error", "hard_error", str(exc), clock)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Layer 8 — CLI Entry Point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hackathon-judge",
        description="Hackathon Judge — sealed, screen-share-friendly judging for project events.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # init
    p_init = sub.add_parser("init", help="Create a new run with rubric config.")
    p_init.add_argument("run_id", help="Unique run identifier.")
    p_init.add_argument("--mode", default="workshop",
                        choices=["workshop", "async", "replay", "compare"],
                        help="Run mode (default: workshop).")
    p_init.add_argument("--config", help="Path to rubric config JSON file.")
    p_init.add_argument("--event", help="Path to a portable EventSpec JSON file.")
    p_init.add_argument("--showtime", action="store_true", help="Add workshop pacing to the live CLI output.")

    # submit
    p_sub = sub.add_parser("submit", help="Add a project submission.")
    p_sub.add_argument("run_id", help="Run identifier.")
    p_sub.add_argument("--builder-name", required=True, dest="builder_name", help="Builder's name.")
    p_sub.add_argument("--project-name", required=True, dest="project_name", help="Project name.")
    p_sub.add_argument("--description", default="", help="Project description.")
    p_sub.add_argument(
        "--problem-statement",
        dest="problem_statement",
        default="",
        help="Builder-provided problem the project addresses.",
    )
    p_sub.add_argument(
        "--intended-user",
        dest="intended_user",
        default="",
        help="Builder-provided intended user or audience.",
    )
    p_sub.add_argument(
        "--demo-url",
        dest="demo_url",
        default="",
        help="Demo, deployed artifact, or supporting project URL.",
    )
    p_sub.add_argument(
        "--builder-notes",
        dest="builder_notes",
        default="",
        help="Optional builder context for evidence-grounded feedback.",
    )
    p_sub.add_argument(
        "--copilot-evidence",
        dest="copilot_evidence",
        help="Builder-provided evidence of Copilot use; never inferred when omitted.",
    )
    p_sub.add_argument(
        "--frontier-evidence",
        dest="frontier_evidence",
        help="Builder-provided evidence of frontier-model or agent use; never inferred when omitted.",
    )
    p_sub.add_argument("--file", action="append", help="Attach a file artifact (may repeat).")
    p_sub.add_argument("--showtime", action="store_true", help="Add workshop pacing to the live CLI output.")

    # import-urls
    p_import = sub.add_parser("import-urls", help="Bulk import GitHub repo URLs as workshop submissions.")
    p_import.add_argument("run_id", help="Run identifier.")
    p_import.add_argument(
        "urls",
        nargs="*",
        help=(
            "GitHub URLs or owner/repo entries; optionally use URL | Team | Copilot "
            "evidence | Frontier evidence | Problem | Intended user | Demo/artifact | Notes."
        ),
    )
    p_import.add_argument(
        "--file",
        help="Text file containing one project entry per line; supports optional pipe-delimited context.",
    )
    p_import.add_argument("--builder-name", default="Hackathon Participants",
                          help="Participant display name for imported repo submissions.")
    p_import.add_argument("--showtime", action="store_true", help="Add workshop pacing to the live CLI output.")

    # quick
    p_quick = sub.add_parser(
        "quick",
        help="Run quiet, private judging and create evidence-based project feedback.",
    )
    p_quick.add_argument(
        "urls",
        nargs="*",
        help="GitHub URLs or owner/repo entries; optionally use URL | Team | Copilot evidence | Frontier evidence.",
    )
    p_quick.add_argument(
        "--file",
        help="Text file containing one project entry per line; supports optional pipe-delimited evidence.",
    )
    p_quick.add_argument("--run-id", dest="run_id", help="Run identifier (default: timestamped).")
    p_quick.add_argument(
        "--builder-name",
        default="Hackathon Participants",
        help="Fallback display name when an entry does not provide a team or builder.",
    )
    p_quick.add_argument("--config", help="Path to rubric config JSON file.")
    p_quick.add_argument("--event", help="Path to a portable EventSpec JSON file.")
    p_quick.add_argument(
        "--tie-resolution",
        action="append",
        help=(
            "Explicit human tie decision, repeatable: "
            "rank:<place>=<submission-id> or award:<award-id>=<submission-id>."
        ),
    )

    # workshop
    p_workshop = sub.add_parser(
        "workshop",
        help="The single Live Show: links → spotlights → audience moment → awards.",
    )
    p_workshop.add_argument(
        "urls",
        nargs="*",
        help="Optional GitHub URLs or owner/repo entries; pipe-delimited team and evidence are supported.",
    )
    p_workshop.add_argument(
        "--file",
        help="Text file containing project entries; supports pipe-delimited project context.",
    )
    p_workshop.add_argument("--run-id", dest="run_id", help="Run identifier (default: timestamped).")
    p_workshop.add_argument("--audience", choices=["external", "internal"], help="Audience context.")
    p_workshop.add_argument("--awards", help=argparse.SUPPRESS)
    p_workshop.add_argument("--panel-style", choices=["fun", "professional"], dest="panel_style", help="Panel voice.")
    p_workshop.add_argument("--config", help="Path to rubric config JSON file.")
    p_workshop.add_argument("--event", help="Path to a portable EventSpec JSON file.")
    p_workshop.add_argument(
        "--tie-resolution",
        action="append",
        help=(
            "Explicit human tie decision, repeatable: "
            "rank:<place>=<submission-id> or award:<award-id>=<submission-id>."
        ),
    )
    p_workshop.add_argument("--showtime", action="store_true", help="Run as a live audience show (default unless --configure).")
    p_workshop.add_argument(
        "--demo",
        action="store_true",
        help="Run the same Live Show as a deterministic practice demo; bundled projects are used when no links are supplied.",
    )
    p_workshop.add_argument("--yes", action="store_true", help="Run non-interactively with defaults.")
    p_workshop.add_argument("--configure", action="store_true", help="Ask advanced setup questions before the show.")
    p_workshop.add_argument("--manual-confirm", action="store_true", dest="manual_confirm",
                            help="Ask before each stage instead of auto-running.")
    p_workshop.add_argument("--no-suspense", action="store_true", dest="no_suspense",
                            help="Disable live countdown pauses for CI or fast demos.")
    p_workshop.add_argument(
        "--projector",
        action="store_true",
        help="Optimize the current Live Show terminal for projection; no second window opens.",
    )
    p_workshop.add_argument(
        "--require-live-terminal",
        action="store_true",
        dest="require_live_terminal",
        help="Block unless the current Live Show output is a real interactive terminal.",
    )
    p_workshop.add_argument(
        "--require-projector-window",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    # judge
    p_judge = sub.add_parser("judge", help="Trigger eval engine.")
    p_judge.add_argument("run_id", help="Run identifier.")
    p_judge.add_argument("--showtime", action="store_true", help="Add workshop pacing to the live CLI output.")

    # present
    p_present = sub.add_parser("present", help="Generate presentation from stored artifacts.")
    p_present.add_argument("run_id", help="Run identifier.")
    p_present.add_argument("--showtime", action="store_true", help="Add workshop pacing to the live CLI output.")
    p_present.add_argument("--projector", action="store_true", help="Big-screen mode for projection.")
    p_present.add_argument("--operator", action="store_true",
                           help="Show revealed scores after awards have been declared.")

    # replay
    p_replay = sub.add_parser("replay", help="Read-only replay of a prior bundle.")
    p_replay.add_argument("bundle", help="Run ID or path to bundle directory or .tar.gz.")
    p_replay.add_argument("--showtime", action="store_true", help="Add workshop pacing to the live CLI output.")

    # resume
    p_resume = sub.add_parser("resume", help="Resume an interrupted judge run.")
    p_resume.add_argument("run_id", help="Run identifier.")

    # compare
    p_compare = sub.add_parser("compare", help="Diff two sealed run bundles.")
    p_compare.add_argument("bundle_a", help="First bundle (run ID or path).")
    p_compare.add_argument("bundle_b", help="Second bundle (run ID or path).")

    # list
    sub.add_parser("list", help="List all runs and statuses.")

    # award
    p_award = sub.add_parser("award", help="Declare winner and write winner card.")
    p_award.add_argument("run_id", help="Run identifier.")
    p_award.add_argument("--winner", required=True, help="Winning submission ID.")
    p_award.add_argument(
        "--tie-resolution",
        action="append",
        help=(
            "Human tie decision, repeatable: "
            "rank:<place>=<submission-id> or award:<award-id>=<submission-id>."
        ),
    )
    p_award.add_argument("--showtime", action="store_true", help="Add workshop pacing to the live CLI output.")
    p_award.add_argument("--no-suspense", action="store_true", dest="no_suspense",
                         help="Disable live countdown pauses for CI or fast demos.")

    # recap
    p_recap = sub.add_parser("recap", help="Write a workshop recap from stored artifacts.")
    p_recap.add_argument("run_id", help="Run identifier.")
    p_recap.add_argument("--out", help="Output Markdown file path (default: <bundle>/recap.md).")

    # tui
    p_tui = sub.add_parser(
        "tui",
        help="Open an optional Textual run monitor; the Live Show never launches it.",
    )
    p_tui.add_argument("run_id", nargs="?", help="Optional run identifier to present.")
    p_tui.add_argument("--showtime", action="store_true", help="Add workshop pacing to the live CLI output.")
    p_tui.add_argument("--projector", action="store_true", help="Use the larger optional-monitor layout.")
    p_tui.add_argument("--operator", action="store_true",
                       help="Show the operator projection after an award reveal.")

    # feedback
    p_fb = sub.add_parser("feedback", help="Generate feedback proposal (human approval required).")
    p_fb.add_argument("run_id", help="Run identifier.")
    p_fb.add_argument("--submission-id", dest="submission_id", help="Specific submission ID (optional).")

    # export
    p_export = sub.add_parser("export", help="Package full immutable bundle.")
    p_export.add_argument("run_id", help="Run identifier.")
    p_export.add_argument("--force", action="store_true",
                          help=argparse.SUPPRESS)

    # validate
    p_val = sub.add_parser("validate", help="Verify bundle HASHES and SEAL integrity.")
    p_val.add_argument("bundle", help="Run ID or path to bundle directory.")

    # doctor
    p_doc = sub.add_parser("doctor", help="Diagnose config, model gate, and bundle health.")
    p_doc.add_argument("run_id", nargs="?", help="Optional run ID to inspect.")

    return parser


COMMAND_MAP = {
    "init": cmd_init,
    "submit": cmd_submit,
    "import-urls": cmd_import_urls,
    "quick": cmd_quick,
    "workshop": cmd_workshop,
    "judge": cmd_judge,
    "present": cmd_present,
    "replay": cmd_replay,
    "resume": cmd_resume,
    "compare": cmd_compare,
    "list": cmd_list,
    "award": cmd_award,
    "recap": cmd_recap,
    "tui": cmd_tui,
    "feedback": cmd_feedback,
    "export": cmd_export,
    "validate": cmd_validate,
    "doctor": cmd_doctor,
}


def main(argv: Optional[List[str]] = None,
         _gateway: Optional[Any] = None,
         clock: Optional[Callable] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    handler = COMMAND_MAP.get(args.command)
    if not handler:
        _print_error(1, "UnknownCommand", f"Unknown command: {args.command}")
        return 1

    try:
        return handler(args, _gateway, clock)
    except HackathonJudgeError as exc:
        _print_error(exc.exit_code, type(exc).__name__, str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 1
    except Exception as exc:
        import traceback
        print(f"[ERROR 1] UnhandledException: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
