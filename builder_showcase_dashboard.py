#!/usr/bin/env python3
"""
Copilot Builder Showcase: Optional Run Monitor (Textual TUI)

A real-time optional terminal monitor for Copilot Builder Showcase. Shows
submissions, review progress, spotlight cards, and award reveals with
animated in-place rendering. The primary showcase never auto-launches it.

The dashboard reads bundles through ``bundle_reader.BundleReader`` and, by
default, only ever sees the audience-safe projection: no scores or ranks
are shown until a run's manifest status reaches ``awarded``/``exported``.
Pass ``--operator`` to see the full, unredacted facilitator view instead.

Usage:
  python3 builder_showcase_dashboard.py <run_id>
  python3 builder_showcase_dashboard.py <run_id> --projector   # big-screen mode
  python3 builder_showcase_dashboard.py <run_id> --operator    # facilitator view
  python3 builder_showcase.py tui <run_id>                      # via main CLI
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import (
    Footer,
    Header,
    Static,
    DataTable,
    RichLog,
)
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align
from rich.console import Group
from rich.columns import Columns

from bundle_reader import BundleReader, BundleView
from builder_showcase import get_bundle_path, project_showcase_badges

LEGACY_RUNS_DIR = Path.home() / ".hackathon_judge" / "runs"
DEFAULT_RUNS_DIR = Path.home() / ".copilot_builder_showcase" / "runs"
if not DEFAULT_RUNS_DIR.exists() and LEGACY_RUNS_DIR.exists():
    DEFAULT_RUNS_DIR = LEGACY_RUNS_DIR

# ── Palette ──
ACCENT = "#b11f4b"
ACCENT_SOFT = "#fd8ea1"
SURFACE = "#292929"
SUCCESS = "#4ade80"
WARNING = "#fbbf24"
DANGER = "#f87171"
MUTED = "#919191"
LINK = "#4da6ff"


def _score_bar_rich(score: float, max_score: float = 10.0, width: int = 20) -> Text:
    ratio = max(0.0, min(1.0, score / max_score)) if max_score > 0 else 0
    filled = round(ratio * width)
    bar = Text()
    color = "green" if ratio >= 0.8 else "yellow" if ratio >= 0.6 else "red"
    bar.append("█" * filled, style=color)
    bar.append("░" * (width - filled), style="dim")
    bar.append(f" {score:.1f}", style=f"bold {color}")
    return bar


def _verdict_cell(
    verdict: Dict, *, show_scores: bool, revealed: bool
) -> Text:
    """Render a row's score state without implying completed judging is pending."""
    if not verdict:
        return Text("✓ entered", style="cyan")
    if show_scores and revealed and "total_score" in verdict:
        score = float(verdict["total_score"])
        cell = Text(f"{score:.1f} ")
        cell.append_text(_score_bar_rich(score, width=10))
        return cell
    if revealed:
        return Text("✓ reviewed", style="green")
    return Text("🕒 in review", style="dim")


def _progress_label(progress: Optional[Dict], default_total: int) -> str:
    """Render only aggregate evaluator state for the shared dashboard."""
    if not isinstance(progress, dict):
        return ""
    if progress.get("status") == "failed":
        return "⚠ evaluation paused"
    submissions = progress.get("submissions", {})
    if not isinstance(submissions, dict):
        submissions = {}
    stage = str(progress.get("stage", "pending")).replace("-", " ")
    return (
        f"{stage} {submissions.get('completed', 0)}/"
        f"{submissions.get('total', default_total)}"
    )


def _submission_table_rows(
    submissions: List[Dict],
    verdict_map: Dict[str, Dict],
    *,
    show_scores: bool,
    revealed: bool,
) -> List[tuple[str, str, str, Text]]:
    """Build arrival-ordered rows so projects appear before judging starts."""
    rows: List[tuple[str, str, str, Text]] = []
    for index, submission in enumerate(submissions, start=1):
        submission_id = submission.get("submission_id")
        verdict = verdict_map.get(submission_id, {})
        rows.append((
            f"{index}.",
            str(submission.get("project_name", "Unknown"))[:28],
            str(submission.get("builder_name", "Unknown"))[:20],
            _verdict_cell(
                verdict,
                show_scores=show_scores,
                revealed=revealed,
            ),
        ))
    return rows


def _time_ago(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return iso_str[:19]


class BundleState:
    """Wraps a BundleReader and holds the most recently loaded projection.

    Defaults to the audience-safe projection. ``operator=True`` unlocks the
    full facilitator view only after a run is awarded or exported; before
    reveal it remains audience-safe.
    """

    def __init__(self, run_id: str, runs_dir: Optional[Path] = None,
                operator: bool = False):
        self.run_id = run_id
        self.runs_dir = runs_dir or Path(
            os.environ.get(
                "CBS_RUNS_DIR",
                os.environ.get("HJ_RUNS_DIR", str(DEFAULT_RUNS_DIR)),
            )
        )
        self.bundle_path = get_bundle_path(run_id, self.runs_dir)
        self.operator = operator
        self.reader = BundleReader(self.bundle_path)
        self.view: BundleView
        self.refresh()

    def refresh(self) -> None:
        self.view = (
            self.reader.operator_view()
            if self.operator and self.reader.is_revealed()
            else self.reader.audience_view()
        )

    @property
    def status(self) -> str:
        return self.view.status

    @property
    def mode(self) -> str:
        return self.view.mode

    @property
    def event_name(self) -> str:
        return self.view.event_name

    @property
    def submissions(self) -> List[Dict]:
        return self.view.submissions

    @property
    def verdicts(self) -> List[Dict]:
        return self.view.verdicts

    @property
    def feedback(self) -> List[Dict]:
        return self.view.feedback

    @property
    def shadow(self) -> Optional[Dict]:
        return self.view.shadow_score

    @property
    def shadow_assessment(self) -> Optional[Dict]:
        return self.view.shadow_assessment

    @property
    def awards(self) -> Optional[Dict]:
        return self.view.awards

    @property
    def gate(self) -> Optional[Dict]:
        return self.view.freshness_gate

    @property
    def evaluation_progress(self) -> Optional[Dict]:
        return self.view.evaluation_progress

    @property
    def command_log(self) -> List[Dict]:
        return self.view.command_log

    @property
    def sub_count(self) -> int:
        return len(self.submissions)

    @property
    def verdict_map(self) -> Dict[str, Dict]:
        return self.view.verdict_map

    @property
    def feedback_map(self) -> Dict[str, Dict]:
        return self.view.feedback_map

    @property
    def model_name(self) -> str:
        if self.gate:
            selected_models = self.gate.get("selected_models")
            if isinstance(selected_models, list) and selected_models:
                return f"{len(selected_models)}-model panel"
            return self.gate.get("selected_model", "unknown")
        return "pending"

    @property
    def is_revealed(self) -> bool:
        return self.view.revealed

    # Kept as an alias for readability where "sealed or later" is meant.
    @property
    def is_sealed(self) -> bool:
        return self.status in ("sealed", "awarded", "exported")


# ── CSS ──

DASHBOARD_CSS = """
Screen {
    background: #1a1a1a;
}

#header-bar {
    dock: top;
    height: 3;
    background: #b11f4b;
    color: #ffffff;
    text-align: center;
    padding: 0 2;
}

#status-strip {
    dock: top;
    height: 3;
    background: #292929;
    color: #dedede;
    padding: 0 2;
}

#main-area {
    height: 1fr;
}

#left-panel {
    width: 1fr;
    min-width: 40;
    padding: 1;
}

#right-panel {
    width: 36;
    min-width: 32;
    padding: 1;
    background: #242424;
    border-left: solid #474747;
}

.card {
    margin-bottom: 1;
    padding: 1;
    background: #2e2e2e;
    border: round #474747;
}

.card-highlight {
    margin-bottom: 1;
    padding: 1;
    background: #2e2e2e;
    border: round #b11f4b;
}

#spotlight {
    height: auto;
    max-height: 20;
}

#award-reveal {
    height: auto;
}

#activity-log {
    height: 1fr;
    min-height: 8;
    background: #1e1e1e;
    border: round #474747;
    padding: 0 1;
}

.projector #header-bar {
    height: 5;
}

.projector .card, .projector .card-highlight {
    padding: 2;
}

DataTable {
    height: auto;
    max-height: 16;
}

Footer {
    background: #292929;
}
"""

PROJECTOR_CSS = """
Screen {
    background: #0d0d0d;
}

#header-bar {
    height: 5;
}

.card, .card-highlight {
    padding: 2;
}

DataTable {
    max-height: 22;
}
"""


class HeaderBar(Static):
    """Top accent banner."""

    def __init__(self, run_id: str, **kwargs):
        super().__init__(**kwargs)
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        yield Static(
            f"🏟️  Copilot Builder Showcase  🏟️\n"
            f"Run: {self.run_id}",
            id="header-text",
        )


class StatusStrip(Static):
    """Live status indicators."""

    status = reactive("loading")
    subs = reactive(0)
    scored = reactive(0)
    model = reactive("pending")
    sealed = reactive(False)
    progress = reactive("")

    def render(self) -> Text:
        t = Text()
        # Status pill
        status_color = {
            "created": "yellow",
            "judging": "cyan",
            "sealed": "green",
            "awarded": "bright_magenta",
            "exported": "blue",
        }.get(self.status, "dim")
        t.append(" ● ", style=f"bold {status_color}")
        t.append(self.status.upper(), style=f"bold {status_color}")
        t.append("  │  ", style="dim")
        # Counts
        t.append(f"📋 {self.subs} submissions", style="white")
        t.append("  │  ", style="dim")
        t.append(f"🎯 {self.scored} reviewed", style="white")
        t.append("  │  ", style="dim")
        # Model
        t.append(f"🧠 {self.model}", style="cyan")
        if self.progress:
            t.append("  │  ", style="dim")
            t.append(f"⏱ {self.progress}", style="yellow")
        if self.sealed:
            t.append("  │  ", style="dim")
            t.append("🔒 SEALED", style="bold green")
        return t


class SpotlightCard(Static):
    """Displays the current spotlight submission."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._data: Optional[Dict] = None
        self._verdict: Optional[Dict] = None
        self._fb: Optional[Dict] = None
        self._show_scores = False

    def set_spotlight(self, sub: Dict, verdict: Optional[Dict] = None,
                      fb: Optional[Dict] = None, show_scores: bool = False) -> None:
        self._data = sub
        self._verdict = verdict
        self._fb = fb
        self._show_scores = show_scores
        self.refresh()

    def render(self) -> Panel | Text:
        if not self._data:
            return Panel(
                Text("Waiting for spotlight…", style="dim italic"),
                title="🌟 Spotlight",
                border_style=MUTED,
            )

        sub = self._data
        meta = sub.get("repo_metadata", {})
        lines = Text()
        awards_revealed = bool(self._verdict) and "total_score" in self._verdict
        show_scores = self._show_scores and awards_revealed

        # Project name
        lines.append(f"🌟 {sub.get('project_name', 'Unknown')}\n", style="bold bright_white")
        lines.append(f"   Built by: {sub.get('builder_name', 'Unknown')}\n", style="cyan")

        if meta.get("description"):
            lines.append(f"   What it does: {meta['description'][:82]}\n", style="dim")

        badges = project_showcase_badges(meta)
        if meta.get("contributors"):
            badges.append(f"👥 {meta['contributors']}")
        if meta.get("open_issues") is not None:
            badges.append(f"📌 {meta['open_issues']} issues")
        if badges:
            lines.append(f"   Project signals: {' · '.join(badges)}\n", style="bright_white")
        if meta.get("homepage"):
            lines.append(f"   Explore: {str(meta['homepage'])[:84]}\n", style=LINK)

        # Verdict — score bar only appears once the run is revealed
        # (audience_view() withholds total_score until then).
        if self._verdict:
            lines.append("\n")
            if show_scores:
                lines.append("   Score: ", style="bold")
                lines.append_text(_score_bar_rich(float(self._verdict.get("total_score", 0))))
                lines.append("\n")
            reactions = self._verdict.get("archetype_verdicts", [])
            # Before awards, show one concise, score-safe panel take rather
            # than a wall of judge notes. The audience projection has already
            # removed score-like prose.
            if not awards_revealed:
                reactions = reactions[:1]
            for av in reactions:
                label = av.get("archetype_name", "") if awards_revealed else "Panel take"
                lines.append(f"   🎙️ {label}: ", style="bright_magenta")
                lines.append(f"{av.get('bright_spot', av.get('perspective', ''))[:80]}\n", style="green")

        # Feedback
        if self._fb:
            for label, field, icon in (
                ("Copilot", "copilot_use", "🧠"),
                ("Frontier", "frontier_use", "🧭"),
            ):
                assessment = self._fb.get(field, {})
                if isinstance(assessment, dict) and assessment.get("status") == "evidenced":
                    lines.append(
                        f"   {icon} {label}: {str(assessment.get('summary', ''))[:86]}\n",
                        style=LINK,
                    )
            if self._fb.get("bright_spot"):
                lines.append(f"\n   ✨ {self._fb['bright_spot'][:90]}\n", style="green")
            if self._fb.get("next_commit") and awards_revealed:
                lines.append(f"   🔜 {self._fb['next_commit'][:90]}\n", style="yellow")
            if awards_revealed:
                copilot_moves = self._fb.get("copilot_next_moves", [])
                if isinstance(copilot_moves, list) and copilot_moves:
                    lines.append(
                        f"   🧠 Copilot next: {str(copilot_moves[0])[:84]}\n",
                        style=LINK,
                    )
                frontier_ideas = self._fb.get("frontier_experiments", [])
                if isinstance(frontier_ideas, list) and frontier_ideas:
                    lines.append(
                        f"   🧭 Frontier idea: {str(frontier_ideas[0])[:84]}\n",
                        style=LINK,
                    )

        border = ACCENT if self._verdict else MUTED
        return Panel(lines, title="🌟 Spotlight", border_style=border)


class AwardReveal(Static):
    """Award ceremony display."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._awards: Optional[Dict] = None

    def set_awards(self, awards_card: Dict) -> None:
        self._awards = awards_card
        self.refresh()

    def render(self) -> Panel | Text:
        if not self._awards:
            return Panel(
                Text("✉️  Envelopes sealed. Awaiting reveal…", style="dim italic"),
                title="🏆 Awards",
                border_style=MUTED,
            )

        lines = Text()
        tie_ceremony = self._awards.get("tie_ceremony", {})
        if isinstance(tie_ceremony, dict):
            policy = tie_ceremony.get("policy", {})
            if isinstance(policy, dict):
                mode = str(policy.get("mode", "shared-podium")).replace("-", " ")
                lines.append(f"\n ⚖ Tie policy: {mode}\n", style="dim italic")
            award_tie_resolutions = tie_ceremony.get("award_tie_resolutions", [])
            if not isinstance(award_tie_resolutions, list):
                award_tie_resolutions = []
            for event in award_tie_resolutions:
                if not isinstance(event, dict):
                    continue
                if event.get("resolution") == "shared-podium":
                    selected_submission_ids = event.get("selected_submission_ids")
                    if not isinstance(selected_submission_ids, list):
                        continue
                    recipient_count = len(selected_submission_ids)
                    lines.append(
                        f"    Shared podium: {recipient_count} projects share "
                        f"{event.get('award_name', 'this placement')}.\n",
                        style="bold yellow",
                    )
                elif event.get("resolution") == "human-declared":
                    lines.append(
                        "    Tied placement resolved by logged human decision.\n",
                        style="yellow",
                    )
                elif event.get("resolution") == "human-resolution-derived":
                    lines.append(
                        "    Remaining tied project advanced by that decision.\n",
                        style="yellow",
                    )
        awards = self._awards.get("awards", [])
        if not isinstance(awards, list):
            awards = []
        for award in awards:
            if not isinstance(award, dict):
                continue
            emoji = award.get("emoji", "🏆")
            name = award.get("award_name", "Award")
            if award.get("shared_placement"):
                name = f"{name} — SHARED PODIUM"
            winner = award.get("winner_builder_name", "Unknown")
            project = award.get("project_name", "Unknown")
            lines.append(f"\n {emoji} {name}\n", style="bold bright_magenta")
            lines.append(f"    → {project}\n", style="bold bright_white")
            lines.append(f"      Built by {winner}\n", style="cyan")
            reason = award.get("reason", "")
            if reason:
                lines.append(f"    {reason[:90]}\n", style="green")
            tagline = award.get("tagline", "")
            if tagline:
                lines.append(f"    \"{tagline}\"\n", style="dim italic")

        return Panel(lines, title="🏆 Awards", border_style=ACCENT)


class SidePanel(Static):
    """Right sidebar showing run info and stats."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._state: Optional[BundleState] = None

    def set_state(self, state: BundleState) -> None:
        self._state = state
        self.refresh()

    def render(self) -> Panel:
        if not self._state:
            return Panel(Text("Loading…", style="dim"), title="📊 Panel")

        s = self._state
        lines = Text()
        lines.append("Run Info\n", style="bold underline bright_white")
        lines.append(f"  ID:     {s.run_id}\n", style="cyan")
        lines.append(f"  Mode:   {s.mode.upper()}\n", style="cyan")
        lines.append(f"  Status: {s.status.upper()}\n", style="bold green" if s.is_sealed else "yellow")

        lines.append(f"\n  Model:  {s.model_name}\n", style="bright_magenta")

        if s.shadow:
            lines.append(f"\n🔒 Ranking Envelope\n", style="bold underline bright_white")
            lines.append(
                f"  Locked: {str(s.shadow.get('locked_at', 'n/a'))[:19]}\n",
                style="green",
            )

        progress = s.evaluation_progress
        if isinstance(progress, dict):
            lines.append("\n⏱ Room Progress\n", style="bold underline bright_white")
            if progress.get("status") == "failed":
                lines.append(
                    "  ⚠ Evaluation paused — facilitator action needed.\n",
                    style="bold red",
                )
            else:
                lines.append(
                    f"  {_progress_label(progress, s.sub_count)} projects\n",
                    style="yellow",
                )
            eta = progress.get("estimated_remaining_seconds")
            if isinstance(eta, int) and progress.get("status") == "running":
                lines.append(f"  ETA: about {eta}s\n", style="dim")

        if s.is_revealed and s.shadow_assessment:
            assessment = s.shadow_assessment
            status = str(assessment.get("status", "clear")).upper()
            style = "green" if status == "CLEAR" else "yellow"
            lines.append("\n🔍 Shadow Analysis\n", style="bold underline bright_white")
            lines.append(
                f"  Diagnostic status: {status}\n",
                style=f"bold {style}",
            )
            lines.append("  Podium impact: none\n", style="dim")

        # Score distribution — withheld until the run is revealed (awarded
        # or exported); the audience-safe view has no total_score before
        # then, and even the operator view avoids showing a leaderboard
        # ahead of the ceremony.
        scored_verdicts = [v for v in s.verdicts if "total_score" in v]
        if s.operator and s.is_revealed and scored_verdicts:
            lines.append(f"\n📊 Final Scores\n", style="bold underline bright_white")
            scores = sorted((float(v["total_score"]) for v in scored_verdicts), reverse=True)
            for i, sc in enumerate(scores):
                rank = ["🥇", "🥈", "🥉"][i] if i < 3 else f" {i+1}."
                lines.append(f"  {rank} ")
                lines.append_text(_score_bar_rich(sc, width=14))
                lines.append("\n")

        # Awards
        if s.is_revealed and s.awards:
            lines.append(f"\n🏆 Awards\n", style="bold underline bright_white")
            tie_ceremony = s.awards.get("tie_ceremony", {})
            if isinstance(tie_ceremony, dict):
                policy = tie_ceremony.get("policy", {})
                if isinstance(policy, dict):
                    mode = str(policy.get("mode", "shared-podium")).replace("-", " ")
                    lines.append(f"  ⚖ Tie policy: {mode}\n", style="dim")
            for a in s.awards.get("awards", []):
                name = a.get("award_name", "")
                if a.get("shared_placement"):
                    name = f"{name} — SHARED"
                lines.append(
                    f"  {a.get('emoji', '🏆')} {name}\n",
                    style="bright_magenta",
                )

        return Panel(lines, title="📊 Panel Info", border_style=MUTED)


class BuilderDashboard(App):
    """Copilot Builder Showcase optional run monitor."""

    CSS = DASHBOARD_CSS
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("s", "cycle_spotlight", "Next Spotlight"),
        ("q", "quit", "Quit"),
    ]

    spotlight_index = reactive(0)

    def __init__(self, run_id: str, projector: bool = False, operator: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.run_id = run_id
        self.projector = projector
        self.operator = operator
        self.state = BundleState(run_id, operator=operator)
        self._auto_refresh: Optional[Timer] = None
        self.title = (
            "Copilot Builder Showcase — Optional Operator Monitor"
            if operator
            else "Copilot Builder Showcase — Optional Run Monitor"
        )

    @property
    def surface_label(self) -> str:
        return (
            "OPTIONAL OPERATOR MONITOR — KEEP PRIVATE"
            if self.operator
            else "OPTIONAL RUN MONITOR — NOT PART OF THE LIVE SHOWCASE"
        )

    def compose(self) -> ComposeResult:
        yield Static(
            f"{self.surface_label}\n"
            f"🏟️  {self.state.event_name}  🏟️\n"
            f"Run: {self.run_id}",
            id="header-bar",
        )
        yield StatusStrip(id="status-strip")
        with Horizontal(id="main-area"):
            with Vertical(id="left-panel"):
                yield DataTable(id="scores-table")
                yield SpotlightCard(id="spotlight", classes="card-highlight")
                yield AwardReveal(id="award-reveal", classes="card")
            with Vertical(id="right-panel"):
                yield SidePanel(id="side-panel", classes="card")
                yield RichLog(id="activity-log", wrap=True, highlight=True)
        yield Footer()

    def on_mount(self) -> None:
        if self.projector:
            self.screen.add_class("projector")

        table = self.query_one("#scores-table", DataTable)
        table.add_columns("#", "Project", "Team", "Verdict")
        table.cursor_type = "row"

        self._update_all()
        self._auto_refresh = self.set_interval(3.0, self._poll_refresh)
        self._log(f"🏟️  {self.surface_label} connected. Watching for updates…")

    def _log(self, msg: str) -> None:
        log = self.query_one("#activity-log", RichLog)
        log.write(Text(f"  {msg}"))

    def _poll_refresh(self) -> None:
        old_status = self.state.status
        old_verdict_count = len(self.state.verdicts)
        self.state.refresh()
        if self.state.status != old_status:
            self._log(f"● Status changed: {old_status} → {self.state.status}")
        if len(self.state.verdicts) != old_verdict_count:
            self._log(f"🎯 Reviews updated: {len(self.state.verdicts)} verdicts")
        self._update_all()

    def _update_all(self) -> None:
        s = self.state

        # Status strip
        strip = self.query_one("#status-strip", StatusStrip)
        strip.status = s.status
        strip.subs = s.sub_count
        strip.scored = len(s.verdicts)
        strip.model = s.model_name
        strip.sealed = s.is_sealed
        progress = s.evaluation_progress
        strip.progress = _progress_label(progress, s.sub_count)

        # Submissions table — presented in arrival order, never by score
        # or rank. The "Verdict" column only shows a score once the run
        # has been revealed (awarded/exported); until then it just shows
        # review status.
        table = self.query_one("#scores-table", DataTable)
        table.clear()
        for row in _submission_table_rows(
            s.submissions,
            s.verdict_map,
            show_scores=s.operator and s.is_revealed,
            revealed=s.is_revealed,
        ):
            table.add_row(*row)

        # Spotlight
        self._update_spotlight()

        # Awards
        award_widget = self.query_one("#award-reveal", AwardReveal)
        if s.is_revealed and s.awards:
            award_widget.set_awards(s.awards)

        # Side panel
        side = self.query_one("#side-panel", SidePanel)
        side.set_state(s)

    def _update_spotlight(self) -> None:
        s = self.state
        widget = self.query_one("#spotlight", SpotlightCard)
        if not s.submissions:
            return
        idx = self.spotlight_index % len(s.submissions)
        sub = s.submissions[idx]
        sid = sub.get("submission_id")
        verdict = s.verdict_map.get(sid)
        fb = s.feedback_map.get(sid)
        widget.set_spotlight(
            sub,
            verdict,
            fb,
            show_scores=s.operator and s.is_revealed,
        )

    def action_refresh(self) -> None:
        self.state.refresh()
        self._update_all()
        self._log("🔄 Manual refresh")

    def action_cycle_spotlight(self) -> None:
        if self.state.submissions:
            self.spotlight_index = (self.spotlight_index + 1) % len(self.state.submissions)
            self._update_spotlight()
            sub = self.state.submissions[self.spotlight_index]
            self._log(f"🌟 Spotlight: {sub.get('project_name', 'Unknown')}")

    def action_quit(self) -> None:
        self.exit()


def main():
    parser = argparse.ArgumentParser(
        description="Copilot Builder Showcase: Optional Run Monitor",
    )
    parser.add_argument("run_id", help="Run ID to display")
    parser.add_argument("--projector", action="store_true",
                        help="Big-screen / projector mode with larger elements")
    parser.add_argument("--operator", action="store_true",
                        help="Show the full, unredacted facilitator view "
                             "instead of the default audience-safe projection")
    args = parser.parse_args()

    app = BuilderDashboard(run_id=args.run_id, projector=args.projector, operator=args.operator)
    app.run()



if __name__ == "__main__":
    main()
