#!/usr/bin/env python3
"""
Copilot Builder — Judging Panel: Live Dashboard (Textual TUI)

A real-time, Agent Pulse-style terminal dashboard for the Copilot Builder
Judging Panel. Shows submissions, scoring progress, spotlight cards,
and award reveals with animated in-place rendering.

Usage:
  python3 builder_dashboard.py <run_id>
  python3 builder_dashboard.py <run_id> --projector   # big-screen mode
  python3 copilot_builder_panel.py tui <run_id>       # via main CLI
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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

DEFAULT_RUNS_DIR = Path.home() / ".copilot_builder_panel" / "runs"

# ── Palette (matches Clawpilot theme spirit) ──
ACCENT = "#b11f4b"
ACCENT_SOFT = "#fd8ea1"
SURFACE = "#292929"
SUCCESS = "#4ade80"
WARNING = "#fbbf24"
DANGER = "#f87171"
MUTED = "#919191"
LINK = "#4da6ff"


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_ndjson(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def _score_bar_rich(score: float, max_score: float = 10.0, width: int = 20) -> Text:
    ratio = max(0.0, min(1.0, score / max_score)) if max_score > 0 else 0
    filled = round(ratio * width)
    bar = Text()
    color = "green" if ratio >= 0.8 else "yellow" if ratio >= 0.6 else "red"
    bar.append("█" * filled, style=color)
    bar.append("░" * (width - filled), style="dim")
    bar.append(f" {score:.1f}", style=f"bold {color}")
    return bar


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
    """Reads and caches all artifacts from a run bundle directory."""

    def __init__(self, run_id: str, runs_dir: Path | None = None):
        self.run_id = run_id
        self.runs_dir = runs_dir or Path(
            __import__("os").environ.get("CBP_RUNS_DIR", str(DEFAULT_RUNS_DIR))
        )
        self.bundle_path = self.runs_dir / run_id
        self.refresh()

    def refresh(self) -> None:
        self.manifest = _load_json(self.bundle_path / "manifest.json") or {}
        self.submissions = _load_ndjson(self.bundle_path / "submissions.ndjson")
        self.verdicts = _load_ndjson(self.bundle_path / "verdicts.ndjson")
        self.feedback = _load_ndjson(self.bundle_path / "feedback.ndjson")
        self.shadow = _load_json(self.bundle_path / "shadow_score.json")
        self.awards = _load_json(self.bundle_path / "winner" / "awards.json")
        self.gate = _load_json(self.bundle_path / "freshness_gate.json")
        self.command_log = _load_ndjson(self.bundle_path / "command_log.ndjson")

    @property
    def status(self) -> str:
        return self.manifest.get("status", "unknown")

    @property
    def mode(self) -> str:
        return self.manifest.get("mode", "workshop")

    @property
    def sub_count(self) -> int:
        return len(self.submissions)

    @property
    def verdict_map(self) -> Dict[str, Dict]:
        return {v.get("submission_id"): v for v in self.verdicts}

    @property
    def feedback_map(self) -> Dict[str, Dict]:
        return {f.get("submission_id"): f for f in self.feedback}

    @property
    def model_name(self) -> str:
        if self.gate:
            return self.gate.get("selected_model", "unknown")
        return "pending"

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
            f"✨  Copilot Builder — Judging Panel  ✨\n"
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
        t.append(f"🎯 {self.scored} scored", style="white")
        t.append("  │  ", style="dim")
        # Model
        t.append(f"🧠 {self.model}", style="cyan")
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

    def set_spotlight(self, sub: Dict, verdict: Optional[Dict] = None,
                      fb: Optional[Dict] = None) -> None:
        self._data = sub
        self._verdict = verdict
        self._fb = fb
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

        # Project name
        lines.append(f"🌟 {sub.get('project_name', 'Unknown')}\n", style="bold bright_white")
        lines.append(f"   Builder: {sub.get('builder_name', 'Unknown')}\n", style="cyan")

        if meta.get("description"):
            lines.append(f"   {meta['description'][:90]}\n", style="dim")

        # Metadata badges
        badges = []
        if meta.get("language"):
            badges.append(f"📝 {meta['language']}")
        if meta.get("stars") is not None:
            badges.append(f"⭐ {meta['stars']}")
        if meta.get("contributors"):
            badges.append(f"👥 {meta['contributors']}")
        if meta.get("open_issues") is not None:
            badges.append(f"📌 {meta['open_issues']} issues")
        if badges:
            lines.append(f"   {' · '.join(badges)}\n", style="bright_white")

        # Verdict
        if self._verdict:
            score = float(self._verdict.get("total_score", 0))
            lines.append("\n")
            lines.append("   Score: ", style="bold")
            lines.append_text(_score_bar_rich(score))
            lines.append("\n")
            for av in self._verdict.get("archetype_verdicts", []):
                lines.append(f"   🎙️ {av.get('archetype_name', '')}: ", style="bright_magenta")
                lines.append(f"{av.get('bright_spot', av.get('perspective', ''))[:80]}\n", style="green")

        # Feedback
        if self._fb:
            if self._fb.get("bright_spot"):
                lines.append(f"\n   ✨ {self._fb['bright_spot'][:90]}\n", style="green")
            if self._fb.get("next_commit"):
                lines.append(f"   🔜 {self._fb['next_commit'][:90]}\n", style="yellow")

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
        for award in self._awards.get("awards", []):
            emoji = award.get("emoji", "🏆")
            name = award.get("award_name", "Award")
            winner = award.get("winner_builder_name", "Unknown")
            project = award.get("project_name", "Unknown")
            lines.append(f"\n {emoji} {name}\n", style="bold bright_magenta")
            lines.append(f"    → {winner} · {project}\n", style="bold bright_white")
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
            lines.append(f"\n🔒 Shadow Score\n", style="bold underline bright_white")
            lines.append(f"  Hash: {str(s.shadow.get('sealed_hash', ''))[:16]}…\n", style="dim")
            lines.append(f"  Sealed: {s.shadow.get('sealed_at', 'n/a')[:19]}\n", style="green")

        # Score distribution
        if s.verdicts:
            lines.append(f"\n📊 Score Distribution\n", style="bold underline bright_white")
            scores = sorted([float(v.get("total_score", 0)) for v in s.verdicts], reverse=True)
            for i, sc in enumerate(scores):
                rank = ["🥇", "🥈", "🥉"][i] if i < 3 else f" {i+1}."
                lines.append(f"  {rank} ")
                lines.append_text(_score_bar_rich(sc, width=14))
                lines.append("\n")

        # Awards
        if s.awards:
            lines.append(f"\n🏆 Awards\n", style="bold underline bright_white")
            for a in s.awards.get("awards", []):
                lines.append(f"  {a.get('emoji', '🏆')} {a.get('award_name', '')}\n", style="bright_magenta")

        return Panel(lines, title="📊 Panel Info", border_style=MUTED)


class BuilderDashboard(App):
    """Copilot Builder — Judging Panel Live Dashboard."""

    CSS = DASHBOARD_CSS
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("s", "cycle_spotlight", "Next Spotlight"),
        ("q", "quit", "Quit"),
    ]

    spotlight_index = reactive(0)

    def __init__(self, run_id: str, projector: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.run_id = run_id
        self.projector = projector
        self.state = BundleState(run_id)
        self._auto_refresh: Optional[Timer] = None

    def compose(self) -> ComposeResult:
        yield Static(
            f"✨  Copilot Builder — Judging Panel  ✨\n"
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
        table.add_columns("Rank", "Project", "Builder", "Score", "Bar")
        table.cursor_type = "row"

        self._update_all()
        self._auto_refresh = self.set_interval(3.0, self._poll_refresh)
        self._log("🏟️  Dashboard connected. Watching for updates…")

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
            self._log(f"🎯 Scores updated: {len(self.state.verdicts)} verdicts")
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

        # Scores table
        table = self.query_one("#scores-table", DataTable)
        table.clear()
        ranked = sorted(s.verdicts, key=lambda v: float(v.get("total_score", 0)), reverse=True)
        for i, v in enumerate(ranked):
            score = float(v.get("total_score", 0))
            rank = ["🥇", "🥈", "🥉"][i] if i < 3 else f" {i+1}."
            ratio = max(0, min(1, score / 10))
            filled = round(ratio * 14)
            bar = "█" * filled + "░" * (14 - filled)
            table.add_row(
                rank,
                v.get("project_name", "Unknown")[:28],
                v.get("builder_name", "Unknown")[:20],
                f"{score:.1f}",
                bar,
            )

        # Spotlight
        self._update_spotlight()

        # Awards
        award_widget = self.query_one("#award-reveal", AwardReveal)
        if s.awards:
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
        widget.set_spotlight(sub, verdict, fb)

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
        description="Copilot Builder — Judging Panel: Live Dashboard",
    )
    parser.add_argument("run_id", help="Run ID to display")
    parser.add_argument("--projector", action="store_true",
                        help="Big-screen / projector mode with larger elements")
    args = parser.parse_args()

    app = BuilderDashboard(run_id=args.run_id, projector=args.projector)
    app.run()


if __name__ == "__main__":
    main()
