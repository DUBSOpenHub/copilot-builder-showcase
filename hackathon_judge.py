#!/usr/bin/env python3
"""
Hackathon Judge — sealed, replayable judging for project events.
Architecture: the run bundle is the canonical unit of record.

Commands: init, submit, judge, present, replay, resume, compare, list,
          award, feedback, export, validate, doctor

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
import copy
import hashlib
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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

VERSION = "2.0.0"
AWARD_SLATE = copy.deepcopy(DEFAULT_EVENT_SPEC["awards"])
AWARD_NAME = next(
    award["name"] for award in AWARD_SLATE if award["id"] == "grand-prize"
)
DEFAULT_REGISTRY_PATH = Path.home() / ".hackathon_judge" / "registry" / "log.ndjson"
DEFAULT_RUNS_DIR = Path.home() / ".hackathon_judge" / "runs"
SCHEMA_VERSION = "1.0"
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

MAX_SUBMISSION_SIZE_DEFAULT = 5 * 1024 * 1024  # 5 MiB

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
    if _showtime_enabled(args):
        import time
        time.sleep(seconds)


def _suspense_enabled(args: Optional[argparse.Namespace] = None) -> bool:
    if not _showtime_enabled(args):
        return False
    if getattr(args, "no_suspense", False):
        return False
    return sys.stdout.isatty() or os.environ.get("HJ_COLOR", "").lower() == "always"


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


# Known approved models (used when no API is available)
APPROVED_MODELS = [
    {"id": "claude-opus-4.7-xhigh",       "tier": 6, "reasoning": "xhigh", "premium": True, "deprecated": False},
    {"id": "claude-opus-4.7-high",        "tier": 5, "reasoning": "high",  "premium": True, "deprecated": False},
    {"id": "claude-opus-4.8",             "tier": 5, "reasoning": "high",  "premium": True, "deprecated": False},
    {"id": "claude-opus-4.7-1m-internal", "tier": 5, "reasoning": "high",  "premium": True, "deprecated": False},
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
                "--json", "nameWithOwner,description,primaryLanguage,stargazerCount,forkCount,updatedAt,url",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=8,
        )
        data = json.loads(proc.stdout)
        lang = data.get("primaryLanguage") or {}
        return {
            "name_with_owner": data.get("nameWithOwner") or owner_repo,
            "description": data.get("description") or "",
            "language": lang.get("name") if isinstance(lang, dict) else None,
            "stars": data.get("stargazerCount"),
            "forks": data.get("forkCount"),
            "updated_at": data.get("updatedAt"),
            "url": data.get("url") or url,
            "source": "gh",
        }
    except Exception:
        return {
            "name_with_owner": owner_repo,
            "description": "",
            "language": None,
            "stars": None,
            "forks": None,
            "updated_at": None,
            "url": url,
            "source": "fallback",
        }


def import_url_submissions(bundle_path: Path, urls: List[str],
                           builder_name: str = "Hackathon Participants",
                           clock: Optional[Callable] = None) -> List[Dict]:
    """Create idempotent submissions from GitHub repo URLs."""
    created: List[Dict] = []
    inputs_dir = bundle_path / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    for url in urls:
        meta = fetch_repo_metadata(url)
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
            "builder_name": builder_name,
            "project_name": owner_repo,
            "description": " · ".join(summary_bits),
            "repo_url": url,
            "repo_metadata": meta,
            "artifacts": [],
            "submitted_at": _now(clock),
            "file_size_bytes": 0,
        }
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


def _event_grand_prize_name(bundle_path: Path) -> str:
    awards = _event_awards(bundle_path)
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

def compute_shadow_score(scored_submissions: List[Dict], rubric: Dict,
                         clock: Optional[Callable] = None) -> Dict:
    """Phase 1: aggregate weighted scores in memory. Returns ShadowScore dict."""
    dimensions = rubric["rubric"]["dimensions"]
    scores: Dict[str, float] = {}

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

    ranking = sorted(scores.keys(), key=lambda s: scores[s], reverse=True)

    return {
        "scores": scores,
        "ranking": ranking,
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
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:8]
    seed = int(digest, 16)

    positive_words = [
        "outstanding", "impressive", "creative", "innovative", "strong",
        "excellent", "well-executed", "thoughtful", "remarkable", "solid",
    ]
    action_words = [
        "explore", "extend", "build", "integrate", "enhance",
        "consider adding", "try connecting", "ship", "leverage", "expand",
    ]
    next_actions = [
        "a live demo endpoint",
        "automated testing for core flows",
        "a usage analytics layer",
        "deeper documentation",
        "an API integration",
        "a feedback collection loop",
        "performance benchmarks",
        "a polished onboarding flow",
    ]

    pw = positive_words[seed % len(positive_words)]
    aw = action_words[(seed >> 4) % len(action_words)]
    na = next_actions[(seed >> 8) % len(next_actions)]

    return json.dumps({
        "bright_spot": f"This project demonstrates {pw} execution and creative thinking.",
        "next_commit": f"Consider your next step: {aw} {na} to take this even further.",
        "panel_notes": (
            f"The panel reviewed this submission carefully. "
            f"Strong innovation is evident throughout the work. "
            f"The builder shows genuine skill and commitment. [model={model_id}, ref={digest}]"
        ),
        "scores": {
            "innovation": (seed % 4) + 7,
            "impact": ((seed >> 2) % 4) + 7,
            "execution": ((seed >> 4) % 4) + 7,
            "presentation": ((seed >> 6) % 4) + 7,
        },
    })


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
            "status": "blocked",
            "policy_mode": policy_mode,
            "reason": f"Model API unavailable: {exc}",
            "checked_at": checked_at,
            "evaluation_provenance": provenance,
        }
        write_once_json(gate_path, result)
        raise ModelAPIError(f"Model API unavailable during freshness gate: {exc}") from exc

    # Find preferred model in available list
    found = next((m for m in available if m["id"] == preferred_model), None)
    is_deprecated = found is not None and found.get("deprecated", False)
    is_missing = found is None
    is_not_premium = bool(found) and required_tier == "premium" and not found.get("premium", False)
    reasoning_order = {"low": 0, "medium": 1, "high": 2, "xhigh": 3}
    required_reasoning_value = reasoning_order.get(str(required_reasoning).lower(), 2)
    found_reasoning_value = reasoning_order.get(str((found or {}).get("reasoning", "low")).lower(), 0)
    is_low_reasoning = bool(found) and found_reasoning_value < required_reasoning_value

    if (is_missing or is_deprecated or is_not_premium or is_low_reasoning):
        failure_reason = (
            "deprecated" if is_deprecated else
            "not available" if is_missing else
            "not premium" if is_not_premium else
            "below required reasoning tier"
        )
        if policy_mode == "strict":
            reason = (
                f"Model '{preferred_model}' is {failure_reason}. "
                f"Policy mode is 'strict' — run blocked."
            )
            result = {
                "configured_model": preferred_model,
                "available_models": available,
                "selected_model": preferred_model,
                "status": "blocked",
                "policy_mode": policy_mode,
                "reason": reason,
                "checked_at": checked_at,
                "evaluation_provenance": provenance,
            }
            write_once_json(gate_path, result)
            raise FreshnessGateBlock(reason)
        else:
            # Permissive: select best non-deprecated model
            best = _select_best_model(available)
            reason = (
                f"Model '{preferred_model}' is {failure_reason}. "
                f"Falling back to '{best}' per permissive policy."
            )
            result = {
                "configured_model": preferred_model,
                "available_models": available,
                "selected_model": best,
                "status": "fallback",
                "policy_mode": policy_mode,
                "reason": reason,
                "fallback_reason": reason,
                "checked_at": checked_at,
            }
            print(f"[WARN] Freshness gate fallback: {reason}", file=sys.stderr)
    else:
        result = {
            "configured_model": preferred_model,
            "available_models": available,
            "selected_model": preferred_model,
            "status": "pass",
            "policy_mode": policy_mode,
            "reason": f"Model '{preferred_model}' is current and approved.",
            "required_tier": required_tier,
            "required_reasoning": required_reasoning,
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
        {"low": 0, "medium": 1, "high": 2, "xhigh": 3}.get(str(m.get("reasoning", "low")).lower(), 0),
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
    """Extended tone check for feedback cards — validates bright_spot and next_commit."""
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

def _parse_model_response(raw: str) -> Dict:
    """Parse model response JSON, with fallback for plain text."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {
            "bright_spot": "This project demonstrates strong and impressive technical execution.",
            "next_commit": "Consider extending the core functionality to reach even more users.",
            "panel_notes": raw[:500] if raw else "The panel reviewed this submission.",
            "scores": {},
        }


def score_submissions(
    submissions: List[Dict],
    rubric: Dict,
    selected_model: str,
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
    scored: List[Dict] = []

    for i, sub in enumerate(submissions):
        sid = sub["submission_id"]
        dimension_scores: Dict[str, Any] = {}

        for j, arch in enumerate(archetypes):
            prompt = _build_scoring_prompt(sub, rubric, arch)
            try:
                raw = call_model(prompt, selected_model, _gateway)
                parsed = _parse_model_response(raw)
            except Exception as exc:
                raise ModelAPIError(f"Model call failed for submission {sid}: {exc}") from exc

            arch_scores = parsed.get("scores", {})
            for dim in dimensions:
                dim_id = dim["id"]
                raw_score = arch_scores.get(dim_id)
                if raw_score is None:
                    # Derive from hash for reproducibility
                    h = int(hashlib.sha256(f"{sid}{dim_id}{arch['id']}".encode()).hexdigest()[:4], 16)
                    raw_score = 7 + (h % 4)
                raw_score = max(0, min(dim["max_score"], int(raw_score)))

                if dim_id not in dimension_scores:
                    dimension_scores[dim_id] = {
                        "score": raw_score,
                        "max_score": dim["max_score"],
                        "rationale": parsed.get("panel_notes", "")[:200],
                        "archetype": arch["id"],
                    }
                else:
                    # Average across archetypes
                    prev = dimension_scores[dim_id]["score"]
                    dimension_scores[dim_id]["score"] = round((prev + raw_score) / 2)

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
            "model": selected_model,
            "timestamp": _now(clock),
        })

    return scored


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
        f"Description: {sub.get('description', '')}\n\n"
        f"Rubric dimensions:\n{dim_list}\n\n"
        "Respond with a JSON object containing:\n"
        '  "scores": { "<dimension_id>": <integer score> },\n'
        '  "bright_spot": "<one positive highlight>",\n'
        '  "next_commit": "<one forward-looking improvement nudge>",\n'
        '  "panel_notes": "<brief supporting rationale>"\n\n'
        "Be celebratory and supportive. Focus on strengths and growth opportunities.\n"
        "Respond with valid JSON only."
    )


def build_panel_verdicts(
    scored_submissions: List[Dict],
    submissions: List[Dict],
    rubric: Dict,
    selected_model: str,
    bundle_path: Path,
    _gateway: Optional[Any] = None,
    clock: Optional[Callable] = None,
) -> List[Dict]:
    """Build per-submission panel verdicts. Writes verdicts/<id>.json."""
    archetypes = rubric.get("judge_archetypes", DEFAULT_RUBRIC["judge_archetypes"])
    sub_map = {s["submission_id"]: s for s in submissions}
    verdicts: List[Dict] = []

    for scored in scored_submissions:
        sid = scored["submission_id"]
        sub = sub_map.get(sid, {})
        archetype_verdicts: List[Dict] = []

        for arch in archetypes:
            prompt = _build_scoring_prompt(sub, rubric, arch)
            try:
                raw = call_model(prompt, selected_model, _gateway)
                parsed = _parse_model_response(raw)
            except Exception as exc:
                raise ModelAPIError(f"Verdict call failed for {sid}: {exc}") from exc

            arch_verdict = {
                "archetype_id": arch["id"],
                "archetype_name": arch["name"],
                "perspective": parsed.get("panel_notes", "A thoughtful submission with notable strengths."),
                "bright_spot": parsed.get("bright_spot", "This project demonstrates impressive technical execution."),
                "scored_at": _now(clock),
            }
            # Tone check each archetype verdict
            tone = check_tone(arch_verdict["perspective"], rubric, f"verdict/{sid}/{arch['id']}", clock)
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


def build_feedback_cards(
    scored_submissions: List[Dict],
    submissions: List[Dict],
    rubric: Dict,
    selected_model: str,
    bundle_path: Path,
    _gateway: Optional[Any] = None,
    clock: Optional[Callable] = None,
) -> List[Dict]:
    """Build per-submission feedback cards. Writes feedback/<id>.json."""
    sub_map = {s["submission_id"]: s for s in submissions}
    cards: List[Dict] = []

    for scored in scored_submissions:
        sid = scored["submission_id"]
        sub = sub_map.get(sid, {})

        # Pull from existing eval steps / model responses for consistency
        prompt = (
            "You are a neutral hackathon judging panel. "
            "Write an encouraging feedback card for this participant.\n\n"
            f"Project: {sub.get('project_name', 'Unknown')}\n"
            f"Builder: {sub.get('builder_name', 'Unknown')}\n"
            f"Description: {sub.get('description', '')}\n\n"
            "Write a JSON feedback card with:\n"
            '  "bright_spot": "<specific positive highlight — what they built well>",\n'
            '  "next_commit": "<one forward-looking, actionable improvement nudge>",\n'
            '  "panel_notes": "<warm, supportive overall note>"\n\n'
            "Be celebratory. Focus on strengths. Use encouraging language only.\n"
            "Respond with valid JSON only."
        )
        try:
            raw = call_model(prompt, selected_model, _gateway)
            parsed = _parse_model_response(raw)
        except Exception as exc:
            raise ModelAPIError(f"Feedback call failed for {sid}: {exc}") from exc

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

        card = {
            "submission_id": sid,
            "builder_name": sub.get("builder_name", ""),
            "bright_spot": bright_spot,
            "next_commit": next_commit,
            "panel_notes": panel_notes,
            "tone_checked": False,
            "delivered_at": _now(clock),
        }

        # Tone check
        tone = check_feedback_card_tone(card, rubric, clock)
        if not tone["passed"]:
            # Attempt safe fallback values
            card["bright_spot"] = "This project demonstrates impressive creativity and strong technical execution."
            card["next_commit"] = "Consider extending your core feature to reach even more users in your next commit."
            card["panel_notes"] = "The panel was inspired by your work. Keep building!"
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
        "artifacts": artifact_refs,
        "submitted_at": _now(clock),
        "file_size_bytes": file_size_bytes,
    }

    sub_path = bundle_path / "inputs" / f"{submission_id}.json"
    write_once_json(sub_path, submission)

    # Update manifest status
    update_status(bundle_path, "collecting", clock)
    log_command(bundle_path, "submit", "ok", f"submission_id={submission_id}", clock)

    _success(f"Submission '{submission_id}' added.")
    _sideline(f"{builder_name} enters the panel with “{project_name}”.", "🌟", "magenta")
    _showtime_pause(args, 0.4)
    return 0


def _read_urls_from_args(args: argparse.Namespace) -> List[str]:
    chunks: List[str] = []
    urls = getattr(args, "urls", None) or []
    if urls:
        chunks.append("\n".join(urls))
    urls_file = getattr(args, "file", None)
    if urls_file:
        chunks.append(Path(urls_file).read_text(encoding="utf-8"))
    if not chunks and not sys.stdin.isatty():
        chunks.append(sys.stdin.read())
    return parse_submission_urls("\n".join(chunks))


def cmd_import_urls(args: argparse.Namespace, _gateway: Optional[Any] = None,
                    clock: Optional[Callable] = None) -> int:
    """import-urls — bulk-create workshop submissions from pasted GitHub URLs."""
    run_id = args.run_id
    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)

    _assert_bundle_exists(bundle_path, run_id)
    manifest = load_manifest(bundle_path)
    _assert_status_in(manifest, ["init", "collecting"], "import-urls")

    urls = _read_urls_from_args(args)
    if not urls:
        _print_error(7, "ConfigValidationError",
                     "No GitHub repo URLs found. Paste URLs, pass --file, or provide owner/repo entries.")
        return 7

    created = import_url_submissions(
        bundle_path,
        urls,
        getattr(args, "builder_name", "Hackathon Participants"),
        clock,
    )
    log_command(bundle_path, "import-urls", "ok", f"created={len(created)} urls={len(urls)}", clock)

    _magic_banner("Project Intake", f"{len(created)} new submissions · {len(urls) - len(created)} already present")
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
    _sideline("Paste GitHub URLs, one per line. Blank line when the room is ready.", "🎤", "magenta")
    lines: List[str] = []
    while True:
        line = input()
        if not line.strip():
            break
        lines.append(line)
    return "\n".join(lines)


def _choose_winner_id(bundle_path: Path) -> Optional[str]:
    shadow = load_shadow_score(bundle_path)
    if shadow and shadow.get("ranking"):
        return shadow["ranking"][0]
    submissions = _load_submissions(bundle_path)
    return submissions[0]["submission_id"] if submissions else None


def _dimension_score(verdict: Dict, dimension_ids: List[str]) -> float:
    if not dimension_ids:
        return float(verdict.get("total_score", 0))
    values: List[float] = []
    for dim_id in dimension_ids:
        ds = verdict.get("dimension_scores", {}).get(dim_id, {})
        if isinstance(ds, dict):
            values.append(float(ds.get("score", 0)))
    return sum(values) / len(values) if values else float(verdict.get("total_score", 0))


def _load_awards(bundle_path: Path) -> Optional[Dict]:
    path = bundle_path / "winner" / "awards.json"
    if not path.exists():
        return None
    return load_json(path)


def _choose_award_winners(bundle_path: Path, builder_winner_id: Optional[str],
                          clock: Optional[Callable] = None) -> Dict:
    submissions = {s["submission_id"]: s for s in _load_submissions(bundle_path)}
    verdicts = {v["submission_id"]: v for v in _load_verdicts(bundle_path)}
    shadow = load_shadow_score(bundle_path) or {}
    ranking = list(shadow.get("ranking") or [])
    if builder_winner_id and builder_winner_id not in ranking:
        ranking.insert(0, builder_winner_id)
    elif builder_winner_id:
        ranking = [builder_winner_id] + [sid for sid in ranking if sid != builder_winner_id]
    if not ranking:
        ranking = sorted(verdicts, key=lambda sid: float(verdicts[sid].get("total_score", 0)), reverse=True)
    if not ranking:
        ranking = list(submissions)

    feedback = {f.get("submission_id"): f for f in _load_feedback(bundle_path)}

    def pick_for(award: Dict) -> str:
        if not award.get("dimensions") and ranking:
            return ranking[0]
        candidates = []
        for sid, verdict in verdicts.items():
            candidates.append((
                _dimension_score(verdict, award.get("dimensions", [])),
                float(verdict.get("total_score", 0)),
                verdict.get("project_name", sid),
                sid,
            ))
        if not candidates:
            return ranking[0] if ranking else next(iter(submissions))
        candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return candidates[0][3]

    awards: List[Dict] = []
    for award in _event_awards(bundle_path):
        sid = pick_for(award)
        sub = submissions.get(sid, {})
        verdict = verdicts.get(sid, {})
        fb = feedback.get(sid, {})
        reason = award.get(
            "reason",
            "This project stood out through a strong response to the event rubric.",
        )
        if not award.get("dimensions") and fb.get("bright_spot"):
            reason = fb["bright_spot"]
        awards.append({
            "award_id": award["id"],
            "award_name": award["name"],
            "emoji": award["emoji"],
            "tagline": award["tagline"],
            "winner_submission_id": sid,
            "winner_builder_name": sub.get("builder_name", verdict.get("builder_name", "Unknown")),
            "project_name": sub.get("project_name", verdict.get("project_name", "Unknown")),
            "reason": reason,
            "score": float(verdict.get("total_score", 0)),
        })

    return {
        "run_id": load_manifest(bundle_path).get("run_id", bundle_path.name),
        "declared_at": _now(clock),
        "requires_human_approval": True,
        "published": False,
        "awards": awards,
    }


def _write_awards_markdown(bundle_path: Path, awards_card: Dict) -> None:
    lines = [f"# {_event_name(bundle_path)} Awards", ""]
    for award in awards_card.get("awards", []):
        lines += [
            f"## {award.get('emoji', '🏆')} {award.get('award_name', 'Award')}",
            "",
            f"**Winner:** {award.get('winner_builder_name', 'Unknown')}  ",
            f"**Project:** {award.get('project_name', 'Unknown')}  ",
            "",
            award.get("tagline", ""),
            "",
            f"**Why it stood out:** {award.get('reason', '')}",
            "",
        ]
    lines += ["> Generated by Hackathon Judge. Human approval is required before external publishing.", ""]
    write_once(bundle_path / "winner" / "awards.md", "\n".join(lines))


def _print_award_ceremony(awards_card: Dict, args: Optional[argparse.Namespace] = None) -> None:
    awards = awards_card.get("awards", [])
    width = min(76, _terminal_width(max_width=80))
    _drumroll("Three awards. Three builder moments. The envelopes are ready.", args)
    for idx, award in enumerate(awards, 1):
        if awards:
            _sideline(f"Envelope {idx}/{len(awards)}", "✉️", "gold")
        _countdown_reveal(args)
        _showtime_pause(args, 0.6)

        emoji = award.get("emoji", "🏆")
        name = award.get("award_name", "Award").upper()
        winner = award.get("winner_builder_name", "Unknown")
        project = award.get("project_name", "Unknown")
        tagline = award.get("tagline", "")
        reason = award.get("reason", "")
        score = award.get("score")

        # Bordered winner card
        print()
        print(_paint("╔" + "═" * width + "╗", "magenta", bold=True))
        print(_paint("║" + f"  {emoji}  {name}".center(width) + "║", "magenta", bold=True))
        print(_paint("╠" + "═" * width + "╣", "magenta", bold=True))
        print(_paint("║" + f"  🌟 Winner:  {winner}".ljust(width) + "║", "gold", bold=True))
        print(_paint("║" + f"  📦 Project: {project}".ljust(width) + "║", "cyan"))
        if score is not None:
            score_line = f"  📊 Score:   {float(score):.1f}/10  {_score_bar(float(score))}"
            # Score bar contains ANSI — print raw
            print(_paint("║  📊 Score:   ", "cyan") + _paint(f"{float(score):.1f}/10  ", "gold", bold=True) + _score_bar(float(score)) + _paint(" " * 2 + "║", "cyan"))
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
            f"→ {award.get('winner_builder_name', 'Unknown')} · {award.get('project_name', 'Unknown')}"
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

    _magic_banner("Workshop Recap", f"{len(verdicts)} repos · {len(awards)} awards · envelope sealed")
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
    print(_paint(f"   Model used:          {gate.get('selected_model', 'unknown')} ({gate.get('status', 'sealed')})", "cyan"))
    print(_paint("   Score envelope:      sealed and replayable", "green", bold=True))
    print()
    print(_paint(f"📦 Bundle: {bundle_path}", "blue"))
    if archive.exists():
        print(_paint(f"📼 Replay archive: {archive}", "blue"))
    if (bundle_path / "recap.md").exists():
        print(_paint(f"📝 Recap: {bundle_path / 'recap.md'}", "blue"))
    if awards_card.get("requires_human_approval", True):
        print(_paint("⚠️  Human approval required before external publishing.", "yellow"))
    _share_card(awards_card, run_id)


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
        ("Bundle validated", "all artifacts intact", cmd_validate, argparse.Namespace(bundle=run_id)),
        ("Replay verified", "stored artifacts only", cmd_replay, argparse.Namespace(bundle=run_id, showtime=False)),
    ]
    for label, detail, fn, ns in steps:
        rc = _run_workshop_tail_step(label, detail, fn, ns, showtime, _gateway, clock)
        if rc:
            return rc
    if showtime:
        _sideline("Envelope sealed. This show is replayable forever.", "🔒", "green")
    return 0


def cmd_workshop(args: argparse.Namespace, _gateway: Optional[Any] = None,
                 clock: Optional[Callable] = None) -> int:
    """workshop — live facilitator flow from repo intake to award reveal."""
    configure = bool(getattr(args, "configure", False))
    manual_confirm = bool(getattr(args, "manual_confirm", False))
    projector = bool(getattr(args, "projector", False))
    showtime = bool(getattr(args, "showtime", False)) or projector or not configure
    assume_yes = bool(getattr(args, "yes", False) or showtime) and not manual_confirm

    _magic_banner(
        "Hackathon Judge Live",
        "Paste project links. Fair judging. A shared celebration.",
    )

    default_run = f"workshop-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    run_id = getattr(args, "run_id", None) or (default_run if not configure else _ask_text("Run name", default_run))

    audience = getattr(args, "audience", None) or (
        "external" if not configure else _ask_choice("Who is in the room?", ["external", "internal"], "external")
    )
    mode = "workshop" if audience == "external" else "async"

    panel_style = getattr(args, "panel_style", None) or (
        "fun" if not configure else _ask_choice("Panel style?", ["fun", "professional"], "fun")
    )

    url_text = "\n".join(getattr(args, "urls", None) or [])
    if getattr(args, "file", None):
        url_text += "\n" + Path(args.file).read_text(encoding="utf-8")
    if not url_text and not sys.stdin.isatty():
        url_text = sys.stdin.read()
    if not url_text:
        url_text = _ask_repo_block()

    urls = parse_submission_urls(url_text)
    if not urls:
        _print_error(7, "ConfigValidationError", "No GitHub repo URLs found for the workshop.")
        return 7

    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)
    show_args = argparse.Namespace(
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
    rc = cmd_init(init_args, _gateway, clock)
    if rc:
        return rc

    event_spec = load_event_spec(bundle_path)
    awards = ", ".join(award["name"] for award in event_spec["awards"])
    _tonight_card(run_id, len(urls), awards, show_args)
    manifest = load_manifest(bundle_path)
    manifest["workshop_choices"] = {
        "audience": audience,
        "awards": [award["name"] for award in event_spec["awards"]],
        "panel_style": panel_style,
        "showtime": showtime,
        "projector": projector,
        "audience_view": event_spec["presentation"]["audience_view"],
        "submission_count_requested": len(urls),
    }
    save_manifest(bundle_path, manifest)

    _act_break("ACT I — PROJECTS ENTER", show_args)
    if assume_yes:
        _sideline(f"{len(urls)} repo URL(s) found. Rolling out the red carpet.", "📋", "cyan")
    elif not _confirm(f"Import {len(urls)} repo submission(s)?"):
        return 0
    created = import_url_submissions(bundle_path, urls, "Hackathon Participants", clock)
    log_command(bundle_path, "workshop-import", "ok", f"created={len(created)} urls={len(urls)}", clock)
    _magic_banner("Project Intake", f"{len(created)} projects entered · {len(urls) - len(created)} already present")
    _project_count_hero(len(created), show_args)
    for sub in created:
        meta = sub.get("repo_metadata", {})
        details = []
        if meta.get("language"):
            details.append(str(meta["language"]))
        if meta.get("stars") is not None:
            details.append(f"⭐ {meta['stars']}")
        suffix = f" — {' · '.join(details)}" if details else ""
        _sideline(f"{sub['project_name']} enters the room{suffix}.", "🌟", "magenta")
        _showtime_pause(show_args, 0.35)

    _act_break("ACT II — THE PANEL SCORES", show_args)
    if assume_yes:
        _sideline("The judges are taking their seats.", "🏟️", "magenta")
    elif not _confirm("Start judging?"):
        return 0
    rc = cmd_judge(argparse.Namespace(run_id=run_id, showtime=showtime, no_suspense=getattr(args, "no_suspense", False)), _gateway, clock)
    if rc:
        return rc

    _act_break("ACT III — SPOTLIGHTS", show_args)
    if assume_yes:
        _sideline("Spotlight round. Every builder gets a moment.", "🎬", "gold")
    elif not _confirm("Open the spotlight round?"):
        return 0
    rc = cmd_present(argparse.Namespace(run_id=run_id, showtime=showtime, no_suspense=getattr(args, "no_suspense", False)), _gateway, clock)
    if rc:
        return rc

    winner_id = _choose_winner_id(bundle_path)
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
        showtime=showtime,
        no_suspense=getattr(args, "no_suspense", False),
    ), _gateway, clock)
    if rc:
        return rc

    should_export = assume_yes or _confirm("Export, validate, recap, and replay the sealed run?")
    if should_export:
        _act_break("ACT V — SEALING THE NIGHT", show_args)
        rc = _seal_the_night(bundle_path, run_id, showtime, _gateway, clock)
        if rc:
            return rc

    _print_workshop_receipt(bundle_path, run_id)
    _sideline("Every builder in this room shipped something today.", "🎉", "green")
    _success(f"Workshop flow complete: {run_id}")
    return 0


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

    showtime = _showtime_enabled(args)

    # Step 2: Freshness gate
    if showtime:
        _magic_banner(event_spec["event"]["name"], event_spec["event"]["tagline"])
        _sideline("Review lenses are ready. Scores stay sealed until the award reveal.", "🏟️", "magenta")
    else:
        _magic_banner(event_spec["event"]["name"], "Premium model policy, sealed scores, and fair review.")
        _sideline("The judging panel is warming up.", "🏟️", "magenta")
    _showtime_pause(args)
    if showtime:
        _sideline("Freshness Gate opening...", "🧭", "cyan")
    else:
        _step(1, 5, "Running Model Freshness Gate...", "🧭")
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

    selected_model = gate_result["selected_model"]
    provenance = gate_result.get("evaluation_provenance", {})
    if showtime:
        _sideline(
            f"Evaluation mode: {provenance.get('mode', 'unknown')} · model: {selected_model}.",
            "🧠",
            "green",
        )
    else:
        _sideline(f"Freshness Gate: {gate_result['status']} — model: {selected_model}", "🧠", "green")
    _showtime_pause(args)

    # Load submissions
    submissions = _load_submissions(bundle_path)
    if not submissions:
        _print_error(7, "ConfigValidationError", "No submissions found. Use 'submit' first.")
        return 7

    update_status(bundle_path, "judging", clock)

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

    if showtime:
        _sideline(f"Scoring the room: {len(remaining)} build(s) under the lights.", "⚖️", "gold")
    else:
        _step(2, 5, f"Scoring {len(remaining)} submission(s) with the panel...", "⚖️")
    _showtime_pause(args)
    def progress(sub: Dict, scored: Dict, index: int, total: int) -> None:
        if not showtime:
            return
        name = _truncate(str(sub.get("project_name", sub.get("submission_id", "Project"))), 38)
        print(_paint(f"   ⬢ {name}", "cyan", bold=True))
        _showtime_pause(args, 0.25)
        print(_paint(f"     Review sealed  [{index}/{total}]", "green"))
        _showtime_pause(args, 0.3)

    try:
        new_scored = score_submissions(
            remaining,
            rubric,
            selected_model,
            bundle_path,
            _gateway,
            clock,
            progress=progress,
        )
    except ModelAPIError as e:
        _print_error(8, "ModelAPIError", str(e))
        return 8

    all_scored = already_scored + new_scored

    # Step 4: Compute & seal shadow score
    if showtime:
        _sideline("Envelope sealed. No score can change from here.", "🔒", "green")
    else:
        _step(3, 5, "Sealing Shadow Score — the envelope closes...", "🔒")
    _showtime_pause(args)
    shadow = compute_shadow_score(all_scored, rubric, clock)
    try:
        seal_shadow_score(bundle_path, shadow, clock)
    except BundleSealError as e:
        # Already sealed — that's fine if we're resuming
        pass

    # Step 5: Build panel verdicts
    if showtime:
        _sideline("Judge reactions locking in for every builder.", "🎙️", "magenta")
    else:
        _step(4, 5, "Writing judge reactions...", "🎙️")
    _showtime_pause(args)
    existing_verdict_sids = {p.stem for p in (bundle_path / "verdicts").glob("*.json")}
    remaining_for_verdicts = [s for s in all_scored if s["submission_id"] not in existing_verdict_sids]
    try:
        build_panel_verdicts(remaining_for_verdicts, submissions, rubric, selected_model,
                             bundle_path, _gateway, clock)
    except (ToneSafetyFailure, ModelAPIError) as e:
        _print_error(e.exit_code, type(e).__name__, str(e))
        return e.exit_code

    # Step 6: Build feedback cards
    if showtime:
        _sideline("Bright spots and next-commit nudges are ready.", "✨", "gold")
    else:
        _step(5, 5, "Preparing bright spots and next-commit nudges...", "✨")
    _showtime_pause(args)
    existing_fb_sids = {p.stem for p in (bundle_path / "feedback").glob("*.json")}
    remaining_for_feedback = [s for s in all_scored if s["submission_id"] not in existing_fb_sids]
    try:
        build_feedback_cards(remaining_for_feedback, submissions, rubric, selected_model,
                             bundle_path, _gateway, clock)
    except (ToneSafetyFailure, ModelAPIError) as e:
        _print_error(e.exit_code, type(e).__name__, str(e))
        return e.exit_code

    update_status(bundle_path, "sealed", clock)
    log_command(bundle_path, "judge", "ok", f"scored={len(all_scored)}", clock)
    _success(f"Judging complete. {len(all_scored)} submission(s) scored and sealed.")
    _sideline("The panel has spoken. The reveal is ready.", "🏁", "gold")
    return 0


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
    showtime = _showtime_enabled(args)
    event_spec = load_event_spec(bundle_path)
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
    _showtime_pause(args)

    # Load and display verdicts (NOT shadow scores)
    verdicts = _load_verdicts(bundle_path)
    if not verdicts:
        print("[INFO] No verdicts found. Run 'judge' first.")
        return 0

    for v in verdicts:
        score = float(v.get("total_score", 0))
        sid = v.get("submission_id")
        sub = sub_map.get(sid, {})
        meta = sub.get("repo_metadata", {})
        badges = []
        if meta.get("language"):
            badges.append(f"📝 {meta['language']}")
        if meta.get("stars") is not None:
            badges.append(f"⭐ {meta['stars']}")
        if meta.get("contributors"):
            badges.append(f"👥 {meta['contributors']}")
        if meta.get("open_issues") is not None:
            badges.append(f"📌 {meta['open_issues']} issues")
        badge_text = f" [{' · '.join(badges)}]" if badges else ""
        project = _truncate(f"{v.get('project_name', sid)}{badge_text}", 68)
        width = min(76, _terminal_width(max_width=80))
        print()
        print(_paint(f"┌─ 🌟 SPOTLIGHT: {project} ", "blue", bold=True) + _paint("─" * max(2, width - len(project) - 16), "blue"))
        print(_paint(f"│ Builder: {v.get('builder_name', 'Unknown')}", "cyan"))
        if meta.get("description"):
            print(_paint(f"│ About:   {_truncate(str(meta.get('description')), 88)}", "cyan"))
        if meta.get("recent_activity"):
            print(_paint(f"│ Recent:  {_truncate(str(meta.get('recent_activity')), 88)}", "cyan"))
        if show_scores:
            print(_paint(f"│ Score:   {score:.2f}/10  {_score_bar(score)}", "gold", bold=True))
        for arch_v in v.get("archetype_verdicts", []):
            print(_paint(f"│ 🎙️ {arch_v['archetype_name']}", "magenta", bold=True))
            print(_paint(f"│    {_truncate(arch_v.get('bright_spot', arch_v.get('perspective', '')), 92)}", "green"))
        fb = feedback.get(v.get("submission_id"), {})
        if fb:
            if fb.get("bright_spot"):
                print(_paint(f"│ ✨ Bright Spot: {_truncate(fb.get('bright_spot', ''), 86)}", "green"))
            if fb.get("next_commit"):
                print(_paint(f"│ 🔜 Next Commit: {_truncate(fb.get('next_commit', ''), 86)}", "yellow"))
        print(_paint("└" + "─" * width, "blue"))
        _showtime_pause(args, 0.5)

    # Show winner if awarded
    awards_card = _load_awards(bundle_path)
    if awards_card:
        print()
        _print_award_ceremony(awards_card, args)
    else:
        _sideline("The envelopes are sealed and waiting for the award reveal.", "🎬", "yellow")

    return 0


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

    awards_card = _choose_award_winners(bundle_path, winner_id, clock)
    declared_at = awards_card["declared_at"]
    grand_prize_name = _event_grand_prize_name(bundle_path)
    winner_card = {
        "run_id": run_id,
        "winner_submission_id": winner_id,
        "winner_builder_name": winner_sub.get("builder_name", "Unknown"),
        "award_name": grand_prize_name,
        "declared_at": declared_at,
        "requires_human_approval": True,
        "published": False,
        "awards": awards_card["awards"],
    }

    # Tone check winner card text
    card_text = (
        f"{winner_sub.get('builder_name', '')} wins the {grand_prize_name} "
        f"for project {winner_sub.get('project_name', '')}."
    )
    tone = check_tone(card_text, load_rubric(bundle_path), "winner_card", clock)
    assert_tone(tone, "winner card")

    write_once_json(winner_path, winner_card)
    write_once_json(bundle_path / "winner" / "awards.json", awards_card)
    _write_awards_markdown(bundle_path, awards_card)
    winner_md = (
        f"# 🏆 {grand_prize_name}\n\n"
        f"**Winner:** {winner_sub.get('builder_name', 'Unknown')}  \n"
        f"**Project:** {winner_sub.get('project_name', 'Unknown')}  \n"
        f"**Run:** `{run_id}`  \n\n"
        "## Why it stood out\n"
        "This project showed a clear story, strong execution, and a compelling demonstration.\n\n"
        "## Next commit nudge\n"
        "Consider adding a short onboarding path so the next builder can try it immediately.\n\n"
        "> Generated by Hackathon Judge. Human approval is required before external publishing.\n"
    )
    write_once(bundle_path / "winner" / "card.md", winner_md)

    # Append registry entry
    registry_path = get_registry_path()
    registry_entry = {
        "run_id": run_id,
        "winner_id": winner_id,
        "award_name": grand_prize_name,
        "declared_at": declared_at,
        "bundle_sha256": "",  # populated after export
    }
    append_ndjson(registry_path, registry_entry)

    # Also append to run-local registry
    local_registry = bundle_path / "registry" / "log.ndjson"
    append_ndjson(local_registry, registry_entry)

    update_status(bundle_path, "awarded", clock)
    log_command(bundle_path, "award", "ok", f"winner={winner_id}", clock)

    _print_award_ceremony(awards_card, args)
    if not _showtime_enabled(args):
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
                f"**Winner:** {award.get('winner_builder_name', 'Unknown')}  ",
                f"**Project:** {award.get('project_name', 'Unknown')}  ",
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
    """tui — live Textual dashboard with graceful CLI fallback."""
    run_id = getattr(args, "run_id", None)
    projector = getattr(args, "projector", False)
    operator = getattr(args, "operator", False)

    # Try launching the Textual dashboard
    if run_id and sys.stdout.isatty():
        try:
            from hackathon_judge_dashboard import BuilderDashboard
            app = BuilderDashboard(run_id=run_id, projector=projector, operator=operator)
            app.run()
            return 0
        except ImportError:
            _warning("Textual not available; falling back to CLI presenter.")
        except Exception as exc:
            _warning(f"Dashboard error ({exc}); falling back to CLI presenter.")

    # Graceful CLI fallback
    if run_id:
        _magic_banner("Hackathon Judge Live Board", "Artifact-powered spotlight mode")
        return cmd_present(args, _gateway, clock)

    _magic_banner("Hackathon Judge Live Board", "Choose a sealed run to present")
    return cmd_list(args, _gateway, clock)


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
    if force:
        _print_error(2, "BundleSealError", "Re-sealing an existing bundle is not supported.")
        return 2
    if seal_path.exists():
        _print_error(2, "BundleSealError",
                     "Bundle already exported (SEAL exists). Create a new run for a new bundle.")
        return 2

    # Update manifest status and log BEFORE computing HASHES so the final state is captured
    update_status(bundle_path, "exported", clock)
    log_command(bundle_path, "export", "ok", "sealing", clock)

    print("  [1/3] Hashing artifacts...")
    _, seal_hash = write_hashes_and_seal(bundle_path)
    print(f"  [1/3] SEAL: {seal_hash[:16]}...")

    print("  [2/3] Creating bundle archive...")
    output_dir = runs_dir
    archive_name = f"{run_id}.bundle.tar.gz"
    archive_path = output_dir / archive_name
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(bundle_path, arcname=run_id)

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
    for line in hashes_content.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2:
            continue
        stored_hash, rel_path = parts
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

    artifact_count = len([l for l in hashes_content.strip().splitlines() if l.strip()])
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
        _sideline(f"Model: {gate.get('selected_model', 'unknown')} ({gate.get('status', '')})", "🧠", "green")

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
                print(_paint(f"    🎙️ {arch_v['archetype_name']}: {arch_v.get('bright_spot', '')[:100]}", "green"))
    else:
        print("\n  No verdicts found in bundle.")

    if feedback:
        print(_paint("\n✨ Next-Commit Nudges", "cyan", bold=True))
        for fc in feedback:
            print(_paint(f"\n  Builder: {fc.get('builder_name', fc['submission_id'])}", "cyan"))
            print(_paint(f"  ✨ {fc.get('bright_spot', '')}", "green"))
            print(_paint(f"  ➜ {fc.get('next_commit', '')}", "yellow"))

    winner_path = bundle_path / "winner" / "card.json"
    awards_card = _load_awards(bundle_path)
    if awards_card:
        print()
        _print_award_ceremony(awards_card, args)
    elif winner_path.exists():
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


def cmd_feedback(args: argparse.Namespace, _gateway: Optional[Any] = None,
                 clock: Optional[Callable] = None) -> int:
    """
    feedback — produce a human-readable feedback proposal.
    Does NOT modify any existing bundle artifact.
    """
    run_id = args.run_id
    submission_id = getattr(args, "submission_id", None)
    runs_dir = get_runs_dir()
    bundle_path = get_bundle_path(run_id, runs_dir)

    _assert_bundle_exists(bundle_path, run_id)
    manifest = load_manifest(bundle_path)
    _assert_status_in(manifest, ["sealed", "awarded", "exported"], "feedback")

    rubric = load_rubric(bundle_path)
    submissions = _load_submissions(bundle_path)

    if submission_id:
        subs_to_process = [s for s in submissions if s["submission_id"] == submission_id]
        if not subs_to_process:
            _print_error(7, "ConfigValidationError", f"Submission '{submission_id}' not found.")
            return 7
    else:
        subs_to_process = submissions

    selected_model = "gpt-4o"
    gate_path = bundle_path / "freshness_gate.json"
    if gate_path.exists():
        gate = load_json(gate_path)
        selected_model = gate.get("selected_model", selected_model)

    proposals: List[Dict] = []
    for sub in subs_to_process:
        sid = sub["submission_id"]
        prompt = (
            f"Generate an enhanced feedback proposal for builder: {sub.get('builder_name', '')}\n"
            f"Project: {sub.get('project_name', '')}\n"
            f"Description: {sub.get('description', '')}\n\n"
            "Provide actionable, encouraging feedback with:\n"
            '  "bright_spot": "<specific strength>",\n'
            '  "next_commit": "<concrete next step>",\n'
            '  "extended_guidance": "<2-3 sentences of supportive coaching>"\n'
            "JSON only. Be celebratory and supportive."
        )
        try:
            raw = call_model(prompt, selected_model, _gateway)
            parsed = _parse_model_response(raw)
        except Exception as exc:
            raise ModelAPIError(f"Feedback proposal call failed: {exc}") from exc

        proposal = {
            "submission_id": sid,
            "builder_name": sub.get("builder_name", ""),
            "project_name": sub.get("project_name", ""),
            "bright_spot": parsed.get("bright_spot", ""),
            "next_commit": parsed.get("next_commit", ""),
            "extended_guidance": parsed.get("panel_notes", parsed.get("extended_guidance", "")),
            "requires_human_approval": True,
            "generated_at": _now(clock),
        }
        proposals.append(proposal)

    # Write proposal file OUTSIDE the bundle (in a feedback_proposals/ dir alongside)
    proposal_dir = runs_dir.parent / "feedback_proposals" / run_id
    proposal_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    proposal_path = proposal_dir / f"proposal_{ts}.json"
    proposal_path.write_text(json.dumps({"proposals": proposals}, indent=2), encoding="utf-8")

    print(f"✓ Feedback proposal written: {proposal_path}")
    print(f"  [NOTE] This proposal requires human approval before delivery.")
    print(f"  {len(proposals)} submission(s) covered.\n")
    for p in proposals:
        print(f"  Builder: {p['builder_name']}")
        print(f"  ✨ {p['bright_spot']}")
        print(f"  → {p['next_commit']}\n")
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

    # 2. Check runs directory
    if runs_dir.exists():
        ok.append(f"Runs directory: {runs_dir}")
    else:
        ok.append(f"Runs directory not yet created: {runs_dir} (will be created on first init)")

    # 3. Check registry
    registry_path = get_registry_path()
    if registry_path.exists():
        entries = read_ndjson(registry_path)
        ok.append(f"Registry: {registry_path} ({len(entries)} entries)")
    else:
        ok.append(f"Registry not yet created: {registry_path} (will be created on first award)")

    # 4. Model gate ping
    try:
        models = query_available_models(_gateway)
        non_deprecated = [m for m in models if not m.get("deprecated", False)]
        ok.append(f"Model gate: {len(models)} models available, {len(non_deprecated)} non-deprecated")
        best = _select_best_model(models)
        ok.append(f"Best available model: {best}")
    except Exception as exc:
        issues.append(f"Model gate: {exc}")

    # 5. Specific bundle check
    if run_id:
        bundle_path = get_bundle_path(run_id, runs_dir)
        if bundle_path.exists():
            manifest = load_manifest(bundle_path)
            status = manifest.get("status", "unknown")
            ok.append(f"Run '{run_id}': status={status}")

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
    p_sub.add_argument("--file", action="append", help="Attach a file artifact (may repeat).")
    p_sub.add_argument("--showtime", action="store_true", help="Add workshop pacing to the live CLI output.")

    # import-urls
    p_import = sub.add_parser("import-urls", help="Bulk import GitHub repo URLs as workshop submissions.")
    p_import.add_argument("run_id", help="Run identifier.")
    p_import.add_argument("urls", nargs="*", help="GitHub URLs or owner/repo entries.")
    p_import.add_argument("--file", help="Text file containing GitHub repo URLs.")
    p_import.add_argument("--builder-name", default="Hackathon Participants",
                          help="Participant display name for imported repo submissions.")
    p_import.add_argument("--showtime", action="store_true", help="Add workshop pacing to the live CLI output.")

    # workshop
    p_workshop = sub.add_parser("workshop", help="Live facilitator flow: paste project links → judge → spotlight → awards.")
    p_workshop.add_argument("urls", nargs="*", help="Optional GitHub URLs or owner/repo entries.")
    p_workshop.add_argument("--file", help="Text file containing GitHub repo URLs.")
    p_workshop.add_argument("--run-id", dest="run_id", help="Run identifier (default: timestamped).")
    p_workshop.add_argument("--audience", choices=["external", "internal"], help="Audience context.")
    p_workshop.add_argument("--awards", help=argparse.SUPPRESS)
    p_workshop.add_argument("--panel-style", choices=["fun", "professional"], dest="panel_style", help="Panel voice.")
    p_workshop.add_argument("--config", help="Path to rubric config JSON file.")
    p_workshop.add_argument("--event", help="Path to a portable EventSpec JSON file.")
    p_workshop.add_argument("--showtime", action="store_true", help="Run as a live audience show (default unless --configure).")
    p_workshop.add_argument("--yes", action="store_true", help="Run non-interactively with defaults.")
    p_workshop.add_argument("--configure", action="store_true", help="Ask advanced setup questions before the show.")
    p_workshop.add_argument("--manual-confirm", action="store_true", dest="manual_confirm",
                            help="Ask before each stage instead of auto-running.")
    p_workshop.add_argument("--no-suspense", action="store_true", dest="no_suspense",
                            help="Disable live countdown pauses for CI or fast demos.")
    p_workshop.add_argument("--projector", action="store_true", help="Big-screen mode for projection.")

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
    p_award.add_argument("--showtime", action="store_true", help="Add workshop pacing to the live CLI output.")
    p_award.add_argument("--no-suspense", action="store_true", dest="no_suspense",
                         help="Disable live countdown pauses for CI or fast demos.")

    # recap
    p_recap = sub.add_parser("recap", help="Write a workshop recap from stored artifacts.")
    p_recap.add_argument("run_id", help="Run identifier.")
    p_recap.add_argument("--out", help="Output Markdown file path (default: <bundle>/recap.md).")

    # tui
    p_tui = sub.add_parser("tui", help="Open the live Textual dashboard (falls back to CLI presenter).")
    p_tui.add_argument("run_id", nargs="?", help="Optional run identifier to present.")
    p_tui.add_argument("--showtime", action="store_true", help="Add workshop pacing to the live CLI output.")
    p_tui.add_argument("--projector", action="store_true", help="Big-screen mode with larger elements for projection.")
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
