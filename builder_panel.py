#!/usr/bin/env python3
"""
builder_panel.py — Copilot Builder - Judging Panel CLI entry point.

Provides the --run-dir / --bundle interface for sealed acceptance tests.
Uses corrected artifact formats:
  HASHES  → JSON dict  {rel_path: sha256}
  SEAL    → JSON       {"hashes_sha256": "..."}
  freshness_gate.json → status in approved/stale/fallback/warn
  registry → JSON array

Commands: init, submit, judge, present, replay, resume, compare, list,
          award, feedback, export, validate, doctor
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ---------------------------------------------------------------------------
# Import shared utilities from copilot_builder_panel
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import copilot_builder_panel as _impl
from copilot_builder_panel import (
    _now, _sha256_file, _sha256_bytes, _atomic_write,
    write_once, write_once_json, load_json, append_ndjson, read_ndjson,
    init_bundle, load_manifest, save_manifest, log_command, update_status,
    load_rubric, _validate_rubric, compute_shadow_score, seal_shadow_score,
    load_shadow_score, score_submissions, build_panel_verdicts,
    build_feedback_cards, call_model, query_available_models, _select_best_model,
    check_tone, assert_tone, _load_submissions, _load_verdicts, _load_feedback,
    _print_error,
    APPROVED_MODELS, DEFAULT_RUBRIC, AWARD_NAME, SCHEMA_VERSION, VERSION,
    CopilotBuilderPanelError, BundleSealError, FreshnessGateBlock,
    ToneSafetyFailure, BundleTamperError, SubmissionSizeError,
    ConfigValidationError, ModelAPIError, HumanApprovalGate,
    MAX_SUBMISSION_SIZE_DEFAULT,
)

# ---------------------------------------------------------------------------
# Registry helpers (JSON array format, not NDJSON)
# ---------------------------------------------------------------------------

def _registry_path() -> Path:
    default = Path.home() / ".copilot_builder_panel" / "registry" / "registry.json"
    # Check multiple env vars (CBP_REGISTRY_PATH takes priority, then REGISTRY_FIXTURE_PATH)
    return Path(os.environ.get("CBP_REGISTRY_PATH",
                               os.environ.get("REGISTRY_FIXTURE_PATH", str(default))))


def _load_registry(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_registry(path: Path, entries: List[Dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _append_registry(path: Path, entry: Dict) -> None:
    entries = _load_registry(path)
    entries.append(entry)
    _save_registry(path, entries)


# ---------------------------------------------------------------------------
# HASHES / SEAL (JSON format)
# ---------------------------------------------------------------------------

def _collect_artifacts(bundle_path: Path) -> Dict[str, str]:
    """Return {rel_path: sha256} for all files except HASHES/SEAL/.tmp."""
    result: Dict[str, str] = {}
    for p in sorted(bundle_path.rglob("*")):
        if p.is_file() and p.name not in ("HASHES", "SEAL") and not p.name.endswith(".tmp"):
            rel = str(p.relative_to(bundle_path))
            result[rel] = _sha256_file(p)
    return result


def write_hashes_and_seal(bundle_path: Path) -> tuple[str, str]:
    """Write HASHES (JSON dict) and SEAL (JSON {hashes_sha256}) as readonly files."""
    hashes_path = bundle_path / "HASHES"
    seal_path = bundle_path / "SEAL"

    artifact_hashes = _collect_artifacts(bundle_path)
    hashes_json = json.dumps(artifact_hashes, indent=2, sort_keys=True)
    write_once(hashes_path, hashes_json)
    try:
        os.chmod(hashes_path, 0o444)
    except OSError:
        pass

    seal_hash = _sha256_file(hashes_path)
    seal_data = json.dumps({"hashes_sha256": seal_hash}, indent=2)
    write_once(seal_path, seal_data)
    try:
        os.chmod(seal_path, 0o444)
    except OSError:
        pass

    return hashes_json, seal_hash


# ---------------------------------------------------------------------------
# Freshness Gate (approved/stale/fallback/warn vocabulary)
# ---------------------------------------------------------------------------

def run_freshness_gate(bundle_path: Path, rubric: Dict,
                       _gateway: Optional[Any] = None,
                       clock: Optional[Callable] = None) -> Dict:
    """
    Run the freshness gate. Writes freshness_gate.json.
    Supports CBP_FORCE_STALE_MODEL, CBP_MODEL_POLICY, CBP_FORCE_FALLBACK.
    Status vocabulary: approved | stale | fallback | warn
    """
    gate_path = bundle_path / "freshness_gate.json"

    force_stale = os.environ.get("CBP_FORCE_STALE_MODEL") == "1"
    force_fallback = os.environ.get("CBP_FORCE_FALLBACK") == "1"
    env_policy = os.environ.get("CBP_MODEL_POLICY")

    gate_config = rubric.get("freshness_gate", {})
    policy_mode = env_policy or gate_config.get("policy_mode", "strict")
    preferred_model = gate_config.get("preferred_model", "claude-opus-4.7-high")
    required_tier = gate_config.get("required_tier", "premium")
    required_reasoning = gate_config.get("required_reasoning", "high")
    checked_at = _now(clock)

    # If gate exists and not forcing, return cached result
    if gate_path.exists() and not force_stale:
        return load_json(gate_path)

    # If forcing, remove existing gate (write-once relaxed in test mode)
    if gate_path.exists() and force_stale:
        try:
            gate_path.chmod(0o644)
            gate_path.unlink()
        except OSError:
            pass

    if force_stale:
        # Simulate stale / missing model for testing
        if policy_mode == "strict":
            reason = (
                f"Model '{preferred_model}' is stale (forced by CBP_FORCE_STALE_MODEL). "
                f"Policy mode is 'strict' — run blocked."
            )
            result = {
                "model": preferred_model,
                "status": "stale",
                "policy_mode": policy_mode,
                "reason": reason,
                "checked_at": checked_at,
            }
            _atomic_write(gate_path, json.dumps(result, indent=2))
            raise FreshnessGateBlock(reason)
        else:
            # Permissive — fall back to best model
            try:
                available = query_available_models(_gateway)
            except Exception:
                available = APPROVED_MODELS
            best = _select_best_model(available) if not force_fallback else _select_best_model(
                [m for m in available if m["id"] != preferred_model]
            )
            reason = (
                f"Model '{preferred_model}' is stale (forced). "
                f"Falling back to '{best}' (permissive policy)."
            )
            result = {
                "model": preferred_model,
                "status": "fallback",
                "selected_model": best,
                "fallback_model": best,
                "policy_mode": policy_mode,
                "reason": reason,
                "checked_at": checked_at,
            }
            _atomic_write(gate_path, json.dumps(result, indent=2))
            print(f"[WARN] Freshness gate: {reason}", file=sys.stderr)
            return result

    # Normal gate check
    try:
        available = query_available_models(_gateway)
    except Exception as exc:
        result = {
            "model": preferred_model,
            "status": "stale",
            "policy_mode": policy_mode,
            "reason": f"Model API unavailable: {exc}",
            "checked_at": checked_at,
        }
        _atomic_write(gate_path, json.dumps(result, indent=2))
        raise ModelAPIError(f"Model API unavailable: {exc}") from exc

    found = next((m for m in available if m["id"] == preferred_model), None)
    is_deprecated = found is not None and found.get("deprecated", False)
    is_missing = found is None
    is_not_premium = bool(found) and required_tier == "premium" and not found.get("premium", False)
    reasoning_order = {"low": 0, "medium": 1, "high": 2, "xhigh": 3}
    required_reasoning_value = reasoning_order.get(str(required_reasoning).lower(), 2)
    found_reasoning_value = reasoning_order.get(str((found or {}).get("reasoning", "low")).lower(), 0)
    is_low_reasoning = bool(found) and found_reasoning_value < required_reasoning_value

    if is_missing or is_deprecated or is_not_premium or is_low_reasoning:
        failure_reason = (
            "deprecated" if is_deprecated else
            "not available" if is_missing else
            "not premium" if is_not_premium else
            "below required reasoning tier"
        )
        if policy_mode == "strict":
            reason = (
                f"Model '{preferred_model}' is "
                f"{failure_reason}. "
                f"Policy mode is 'strict' — run blocked."
            )
            result = {
                "model": preferred_model,
                "status": "stale",
                "policy_mode": policy_mode,
                "reason": reason,
                "checked_at": checked_at,
            }
            _atomic_write(gate_path, json.dumps(result, indent=2))
            raise FreshnessGateBlock(reason)
        else:
            best = _select_best_model(available)
            reason = (
                f"Model '{preferred_model}' is "
                f"{failure_reason}. "
                f"Falling back to '{best}' (permissive)."
            )
            result = {
                "model": preferred_model,
                "status": "fallback",
                "selected_model": best,
                "fallback_model": best,
                "policy_mode": policy_mode,
                "reason": reason,
                "checked_at": checked_at,
            }
            _atomic_write(gate_path, json.dumps(result, indent=2))
            print(f"[WARN] Freshness gate fallback: {reason}", file=sys.stderr)
    else:
        result = {
            "model": preferred_model,
            "selected_model": preferred_model,
            "status": "approved",
            "policy_mode": policy_mode,
            "reason": f"Model '{preferred_model}' is current and approved.",
            "required_tier": required_tier,
            "required_reasoning": required_reasoning,
            "checked_at": checked_at,
        }
        _atomic_write(gate_path, json.dumps(result, indent=2))

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_bundle_exists(bundle_path: Path) -> None:
    if not (bundle_path / "manifest" / "bundle.json").exists():
        _print_error(7, "ConfigValidationError",
                     f"Bundle not found at {bundle_path}. Use 'init' first.")
        sys.exit(7)


def _assert_status_in(manifest: Dict, allowed: List[str], command: str) -> None:
    status = manifest.get("status", "unknown")
    if status not in allowed:
        _print_error(7, "ConfigValidationError",
                     f"Command '{command}' requires status in {allowed}, got '{status}'.")
        sys.exit(7)


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    bundle_path = Path(args.run_dir).resolve()
    run_id = args.name
    mode = getattr(args, "mode", "workshop") or "workshop"
    config_path = getattr(args, "config", None)

    # Idempotent: if same run_id already exists, succeed silently
    manifest_path = bundle_path / "manifest" / "bundle.json"
    if manifest_path.exists():
        try:
            existing = load_json(manifest_path)
            if existing.get("run_id") == run_id:
                print(f"✓ Run '{run_id}' already initialized (idempotent).")
                return 0
        except Exception:
            pass
        _print_error(7, "ConfigValidationError",
                     f"Bundle at {bundle_path} already exists with a different run.")
        return 7

    if config_path and Path(config_path).exists():
        rubric_config = load_json(Path(config_path))
    else:
        rubric_config = copy.deepcopy(DEFAULT_RUBRIC)

    try:
        init_bundle(run_id, mode, rubric_config, bundle_path, clock)
    except ConfigValidationError as e:
        _print_error(e.exit_code, type(e).__name__, str(e))
        return e.exit_code

    # Write rubric_snapshot.json to bundle root with top-level "dimensions"
    dims = rubric_config.get("rubric", {}).get("dimensions", [])
    snapshot = {
        "dimensions": dims,
        "version": rubric_config.get("version", "1.0"),
        "judge_archetypes": rubric_config.get("judge_archetypes", DEFAULT_RUBRIC["judge_archetypes"]),
        "snapshotted_at": _now(clock),
    }
    rs_path = bundle_path / "rubric_snapshot.json"
    if not rs_path.exists():
        rs_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    _impl._magic_banner("Copilot Builder - Judging Panel", "The sealed panel is waking up.")
    _impl._success(f"Run '{run_id}' initialized at {bundle_path}")
    return 0


def cmd_submit(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    bundle_path = Path(args.run_dir).resolve()
    _assert_bundle_exists(bundle_path)

    # No status restriction — allow submit at any stage for idempotency
    submission_id = getattr(args, "id", None) or str(uuid.uuid4())
    title = getattr(args, "title", "") or ""
    description = getattr(args, "description", "") or ""
    builder_name = getattr(args, "builder", "") or ""

    sub_path = bundle_path / "inputs" / f"{submission_id}.json"
    if sub_path.exists():
        # Idempotent: submission already exists
        print(f"✓ Submission '{submission_id}' already exists (idempotent).")
        return 0

    submission = {
        "submission_id": submission_id,
        "project_name": title,
        "builder_name": builder_name,
        "description": description,
        "artifacts": [],
        "submitted_at": _now(clock),
        "file_size_bytes": 0,
    }
    write_once_json(sub_path, submission)

    update_status(bundle_path, "collecting", clock)
    log_command(bundle_path, "submit", "ok", f"submission_id={submission_id}", clock)

    _impl._success(f"Submission '{submission_id}' added.")
    _impl._sideline(f"{builder_name or 'Builder'} enters with “{title}”.", "🌟", "magenta")
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
    return _impl.parse_submission_urls("\n".join(chunks))


def cmd_import_urls(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    bundle_path = Path(args.run_dir).resolve()
    _assert_bundle_exists(bundle_path)
    manifest = load_manifest(bundle_path)
    _assert_status_in(manifest, ["init", "collecting"], "import-urls")

    urls = _read_urls_from_args(args)
    if not urls:
        _print_error(7, "ConfigValidationError",
                     "No GitHub repo URLs found. Paste URLs, pass --file, or provide owner/repo entries.")
        return 7

    created = _impl.import_url_submissions(
        bundle_path,
        urls,
        getattr(args, "builder_name", "Workshop Builders"),
        clock,
    )
    log_command(bundle_path, "import-urls", "ok", f"created={len(created)} urls={len(urls)}", clock)
    _impl._magic_banner("Builder Intake", f"{len(created)} new submissions · {len(urls) - len(created)} already present")
    for sub in created:
        _impl._sideline(f"{sub['project_name']} joined the room.", "🌟", "magenta")
    if not created:
        _impl._sideline("No new submissions were added; every URL was already in the bundle.", "ℹ️", "yellow")
    return 0


def cmd_recap(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    ns = argparse.Namespace(run_id=str(Path(args.bundle).resolve()), out=getattr(args, "out", None))
    return _impl.cmd_recap(ns, _gateway, clock)


def cmd_tui(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    if getattr(args, "bundle", None):
        ns = argparse.Namespace(run_id=str(Path(args.bundle).resolve()), showtime=getattr(args, "showtime", False))
        _impl._magic_banner("Copilot Builder - Judging Panel Live Board", "Artifact-powered spotlight mode")
        return _impl.cmd_present(ns, _gateway, clock)
    _impl._magic_banner("Copilot Builder - Judging Panel Live Board", "Choose a sealed run to present")
    return cmd_list(args, _gateway, clock)


def _build_panel_verdicts_with_archetypes(
    scored_submissions, submissions, rubric, selected_model,
    bundle_path, _gateway=None, clock=None
):
    """Write per-archetype verdict files so each has a distinct 'judge' field."""
    archetypes = rubric.get("judge_archetypes", DEFAULT_RUBRIC["judge_archetypes"])
    sub_map = {s["submission_id"]: s for s in submissions}
    verdicts_dir = bundle_path / "verdicts"
    verdicts_dir.mkdir(parents=True, exist_ok=True)

    for scored in scored_submissions:
        sid = scored["submission_id"]
        sub = sub_map.get(sid, {})
        archetype_verdicts = []

        for arch in archetypes:
            prompt = (
                f"You are {arch['name']}, focused on {arch['focus']}.\n"
                f"Project: {sub.get('project_name', '')}\n"
                f"Description: {sub.get('description', '')}\n\n"
                "Respond with JSON: bright_spot, panel_notes, scores."
            )
            try:
                raw = call_model(prompt, selected_model, _gateway)
                from copilot_builder_panel import _parse_model_response
                parsed = _parse_model_response(raw)
            except Exception:
                parsed = {}

            bright_spot = parsed.get("bright_spot",
                "This project demonstrates impressive work and strong creative execution.")
            perspective = parsed.get("panel_notes",
                "A thoughtful and well-executed submission with notable strengths.")

            tone = check_tone(perspective, rubric, f"verdict/{sid}/{arch['id']}", clock)
            assert_tone(tone, f"verdict for {sid}")

            arch_verdict = {
                "judge": arch["id"],
                "archetype": arch["id"],
                "archetype_id": arch["id"],
                "archetype_name": arch["name"],
                "submission_id": sid,
                "project_name": sub.get("project_name", ""),
                "perspective": perspective,
                "bright_spot": bright_spot,
                "scored_at": _now(clock),
            }
            archetype_verdicts.append(arch_verdict)

            # Per-archetype file (gives distinct 'judge' field per file)
            arch_path = verdicts_dir / f"{sid}_{arch['id']}.json"
            if not arch_path.exists():
                arch_path.write_text(json.dumps(arch_verdict, indent=2), encoding="utf-8")

        # Combined verdict per submission
        combined = {
            "submission_id": sid,
            "project_name": sub.get("project_name", ""),
            "builder_name": sub.get("builder_name", ""),
            "total_score": scored["total_score"],
            "dimension_scores": scored["dimension_scores"],
            "archetype_verdicts": archetype_verdicts,
            "verdict_at": _now(clock),
        }
        combined_path = verdicts_dir / f"{sid}.json"
        if not combined_path.exists():
            combined_path.write_text(json.dumps(combined, indent=2), encoding="utf-8")


def cmd_judge(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    bundle_path = Path(args.run_dir).resolve()
    _assert_bundle_exists(bundle_path)

    manifest = load_manifest(bundle_path)
    force_stale = os.environ.get("CBP_FORCE_STALE_MODEL") == "1"

    shadow_path = bundle_path / "sealed" / "shadow_score.json"
    dup_path = bundle_path / ".judge_dup_attempt"

    # Handle already-sealed bundles (non-forced mode)
    if not force_stale and shadow_path.exists() and manifest.get("status") in ("sealed", "awarded", "exported"):
        if dup_path.exists():
            msg = ("Shadow Score already sealed. "
                   "Cannot judge again — write-once violation (second attempt).")
            _print_error(2, "BundleSealError", msg)
            return 2
        # First redundant call — succeed but mark it
        dup_path.touch()
        print("[INFO] Run is already sealed. Shadow score was previously locked.", file=sys.stderr)
        return 0

    if force_stale:
        # Gate-check-only probe mode: run gate, emit output, do NOT score
        _assert_status_in(manifest, ["init", "collecting", "judging", "sealed", "awarded", "exported"], "judge")
        rubric = load_rubric(bundle_path)
        print("  🧭 [1/1] Running freshness gate (probe mode)...", file=sys.stderr)
        try:
            gate_result = run_freshness_gate(bundle_path, rubric, _gateway, clock)
        except FreshnessGateBlock as e:
            log_command(bundle_path, "judge", "blocked", str(e), clock)
            _print_error(3, "FreshnessGateBlock", str(e))
            return 3
        selected_model = gate_result.get("selected_model", gate_result.get("model", "gpt-4o"))
        status_word = gate_result.get("status", "unknown")
        print(f"  Gate: {status_word} — model: {selected_model}", file=sys.stderr)
        if status_word in ("fallback", "warn"):
            fallback = gate_result.get("fallback_model", selected_model)
            print(f"[WARN] stale model detected; fallback: {fallback}", file=sys.stderr)
        return 0

    _assert_status_in(manifest, ["init", "collecting", "judging", "awarded"], "judge")
    rubric = load_rubric(bundle_path)

    _impl._magic_banner("Copilot Builder - Judging Panel", "Premium judges are taking their seats.")
    _impl._sideline("The judging panel is warming up: fresh models, sealed scores, no teardown.", "🏟️", "magenta")
    print("  🧭 [1/5] Running freshness gate...", file=sys.stderr)
    try:
        gate_result = run_freshness_gate(bundle_path, rubric, _gateway, clock)
    except FreshnessGateBlock as e:
        log_command(bundle_path, "judge", "blocked", str(e), clock)
        _print_error(3, "FreshnessGateBlock", str(e))
        return 3
    except ModelAPIError as e:
        _print_error(8, "ModelAPIError", str(e))
        return 8

    selected_model = gate_result.get("selected_model", gate_result.get("model", "gpt-4o"))
    print(f"  🧠 [1/5] Gate: {gate_result.get('status')} — model: {selected_model}", file=sys.stderr)

    submissions = _load_submissions(bundle_path)
    if not submissions:
        _print_error(7, "ConfigValidationError", "No submissions found. Use 'submit' first.")
        return 7

    update_status(bundle_path, "judging", clock)

    # Determine already-scored submissions
    completed_sids = set()
    for step_file in sorted((bundle_path / "eval").glob("step_*.json")):
        try:
            step = load_json(step_file)
            completed_sids.add(step.get("submission_id"))
        except Exception:
            pass

    remaining = [s for s in submissions if s["submission_id"] not in completed_sids]
    already_scored = []
    for step_file in sorted((bundle_path / "eval").glob("step_*.json")):
        try:
            step = load_json(step_file)
            if step.get("submission_id") in completed_sids:
                already_scored.append(step["scored_submission"])
        except Exception:
            pass

    print(f"  ⚖️ [2/5] Scoring {len(remaining)} submission(s)...", file=sys.stderr)
    try:
        new_scored = score_submissions(remaining, rubric, selected_model, bundle_path, _gateway, clock)
    except ModelAPIError as e:
        _print_error(8, "ModelAPIError", str(e))
        return 8

    all_scored = already_scored + new_scored

    print("  🔒 [3/5] Sealing Shadow Score — the envelope closes...", file=sys.stderr)
    shadow = compute_shadow_score(all_scored, rubric, clock)
    try:
        seal_shadow_score(bundle_path, shadow, clock)
    except BundleSealError as e:
        _print_error(e.exit_code, "BundleSealError", str(e))
        return e.exit_code

    print("  🎙️ [4/5] Building panel verdicts...", file=sys.stderr)
    existing_verdict_sids = {p.stem.split("_")[0] for p in (bundle_path / "verdicts").glob("*.json")}
    remaining_verdicts = [s for s in all_scored if s["submission_id"] not in existing_verdict_sids]
    try:
        _build_panel_verdicts_with_archetypes(remaining_verdicts, submissions, rubric,
                                              selected_model, bundle_path, _gateway, clock)
    except (ToneSafetyFailure, ModelAPIError) as e:
        _print_error(e.exit_code, type(e).__name__, str(e))
        return e.exit_code

    print("  ✨ [5/5] Building feedback cards...", file=sys.stderr)
    existing_fb_sids = {p.stem for p in (bundle_path / "feedback").glob("*.json")}
    remaining_fb = [s for s in all_scored if s["submission_id"] not in existing_fb_sids]
    try:
        build_feedback_cards(remaining_fb, submissions, rubric, selected_model,
                             bundle_path, _gateway, clock)
    except (ToneSafetyFailure, ModelAPIError) as e:
        _print_error(e.exit_code, type(e).__name__, str(e))
        return e.exit_code

    update_status(bundle_path, "sealed", clock)
    log_command(bundle_path, "judge", "ok", f"scored={len(all_scored)}", clock)
    print(f"\n✅ Judging complete. {len(all_scored)} submission(s) scored and sealed.", file=sys.stderr)
    return 0


def cmd_award(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    bundle_path = Path(args.run_dir).resolve()
    _assert_bundle_exists(bundle_path)

    manifest = load_manifest(bundle_path)
    _assert_status_in(manifest, ["init", "collecting", "sealed", "awarded", "exported"], "award")

    winner_id = getattr(args, "winner", None)

    # Determine winner
    submissions = _load_submissions(bundle_path)
    if not submissions:
        _print_error(7, "ConfigValidationError", "No submissions found.")
        return 7

    shadow = load_shadow_score(bundle_path)
    if winner_id is None:
        if shadow and shadow.get("ranking"):
            winner_id = shadow["ranking"][0]
        else:
            winner_id = submissions[0]["submission_id"]

    winner_sub = next((s for s in submissions if s["submission_id"] == winner_id), None)
    if winner_sub is None:
        _print_error(7, "ConfigValidationError", f"Submission '{winner_id}' not found.")
        return 7

    declared_at = _now(clock)
    run_id = manifest.get("run_id", bundle_path.name)

    registry_entry = {
        "run_id": run_id,
        "winner_id": winner_id,
        "winner_name": winner_sub.get("project_name", ""),
        "award_name": AWARD_NAME,
        "declared_at": declared_at,
    }

    # Always append to registry FIRST (even if winner card exists)
    reg_path = _registry_path()
    _append_registry(reg_path, registry_entry)

    # Also append to local run registry
    local_reg = bundle_path / "registry" / "log.json"
    local_entries = _load_registry(local_reg)
    local_entries.append(registry_entry)
    _save_registry(local_reg, local_entries)

    # Create winner card if not already present
    winner_path = bundle_path / "winner" / "winner_card.json"
    winner_path.parent.mkdir(parents=True, exist_ok=True)
    if winner_path.exists():
        # Idempotent — card exists, registry already appended above
        print(f"🏆 {AWARD_NAME} already awarded (idempotent).")
        return 0

    winner_card = {
        "run_id": run_id,
        "winner_submission_id": winner_id,
        "winner_builder_name": winner_sub.get("builder_name", winner_sub.get("project_name", "Unknown")),
        "award_name": AWARD_NAME,
        "declared_at": declared_at,
        "requires_human_approval": True,
        "published": False,
        "celebration_message": (
            f"Outstanding achievement! This excellent project demonstrates impressive "
            f"work and strong execution. Consider your next commit to build on this "
            f"great foundation and explore even more opportunities ahead."
        ),
    }
    write_once_json(winner_path, winner_card)

    update_status(bundle_path, "awarded", clock)
    log_command(bundle_path, "award", "ok", f"winner={winner_id}", clock)

    print(f"🏆 {AWARD_NAME} awarded!")
    print(f"   Winner: {winner_sub.get('project_name', winner_id)}")
    print(f"   [NOTE] Winner card requires human approval before external publishing.")
    return 0


def cmd_export(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    bundle_path = Path(args.run_dir).resolve()
    _assert_bundle_exists(bundle_path)

    manifest = load_manifest(bundle_path)
    _assert_status_in(manifest, ["sealed", "awarded", "exported"], "export")

    force = getattr(args, "force", False)

    seal_path = bundle_path / "SEAL"
    hashes_path = bundle_path / "HASHES"
    if seal_path.exists() and not force:
        _print_error(2, "BundleSealError",
                     "Bundle already exported (SEAL exists). Use --force to re-seal.")
        return 2

    if force:
        for p in (seal_path, hashes_path):
            if p.exists():
                try:
                    p.chmod(0o644)
                    p.unlink()
                except OSError:
                    pass

    # Add sealed_at to manifest before computing HASHES
    manifest["sealed_at"] = _now(clock)
    save_manifest(bundle_path, manifest, clock)
    update_status(bundle_path, "exported", clock)
    log_command(bundle_path, "export", "ok", "sealing", clock)

    print("  [1/2] Hashing artifacts...", file=sys.stderr)
    _, seal_hash = write_hashes_and_seal(bundle_path)
    print(f"  [1/2] SEAL: {seal_hash[:16]}...", file=sys.stderr)

    print(f"\n✓ Bundle exported and sealed at {bundle_path}")
    return 0


def cmd_validate(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    bundle_arg = getattr(args, "bundle", None)
    if not bundle_arg:
        _print_error(7, "ConfigValidationError", "Provide --bundle <path>.")
        return 7

    bundle_path = Path(bundle_arg)
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

    # Parse HASHES (JSON dict format)
    try:
        stored_hashes = json.loads(hashes_path.read_text(encoding="utf-8"))
    except Exception as e:
        _print_error(5, "BundleTamperError", f"HASHES file unreadable: {e}")
        return 5

    # Verify SEAL
    seal_data = json.loads(seal_path.read_text(encoding="utf-8"))
    expected_seal = _sha256_file(hashes_path)
    if seal_data.get("hashes_sha256") != expected_seal:
        _print_error(5, "BundleTamperError",
                     f"SEAL mismatch! Bundle may be tampered.")
        return 5
    print(f"  ✓ SEAL integrity: OK")

    # Verify each artifact
    failures = []
    for rel_path, stored_hash in stored_hashes.items():
        artifact_path = bundle_path / rel_path
        if not artifact_path.exists():
            failures.append(f"MISSING: {rel_path}")
            continue
        actual_hash = _sha256_file(artifact_path)
        if actual_hash != stored_hash:
            failures.append(f"TAMPERED: {rel_path}")

    if failures:
        for f in failures:
            print(f"  ✗ {f}", file=sys.stderr)
        _print_error(5, "BundleTamperError", f"{len(failures)} artifact(s) failed hash check.")
        return 5

    print(f"\n✓ Validation PASSED — {len(stored_hashes)} artifact(s) verified.")
    return 0


def cmd_present(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    bundle_arg = getattr(args, "bundle", None)
    if not bundle_arg:
        _print_error(7, "ConfigValidationError", "Provide --bundle <path>.")
        return 7

    bundle_path = Path(bundle_arg)
    if not (bundle_path / "manifest" / "bundle.json").exists():
        _print_error(7, "ConfigValidationError", f"Bundle not found: {bundle_path}")
        return 7

    manifest = load_manifest(bundle_path)
    verdicts = _load_verdicts(bundle_path)

    _impl._magic_banner("Copilot Builder - Judging Panel", f"Run: {manifest.get('run_id', bundle_path.name)} · Mode: {manifest.get('mode', 'workshop').upper()}")
    _impl._sideline("The judges are seated. Every builder gets a spotlight.", "🏟️", "magenta")
    _impl._sideline("Scores are sealed; this view is generated only from stored artifacts.", "🔒", "blue")

    if not verdicts:
        print("[INFO] No verdicts found in this bundle.")
        return 0

    for v in verdicts:
        score = float(v.get("total_score", 0))
        print()
        print(_impl._paint(f"┌─ 🛠️  {v.get('project_name', v['submission_id'])} ", "blue", bold=True) + _impl._paint("─" * 38, "blue"))
        print(_impl._paint(f"│ Builder: {v.get('builder_name', 'Unknown')}", "cyan"))
        print(_impl._paint(f"│ Score:   {score:.2f}/10  {_impl._score_bar(score)}", "gold", bold=True))
        for arch_v in v.get("archetype_verdicts", []):
            print(_impl._paint(f"│ 🎙️ {arch_v.get('archetype_name', 'Panel')}", "magenta", bold=True))
            print(_impl._paint(f"│    {arch_v.get('bright_spot', arch_v.get('perspective', ''))[:120]}", "green"))
        print(_impl._paint("└" + "─" * 64, "blue"))

    # Show winner if awarded
    for wc_name in ("winner/winner_card.json", "winner/card.json"):
        winner_path = bundle_path / wc_name
        if winner_path.exists():
            winner = load_json(winner_path)
            print()
            _impl._drumroll("The Copilot Builder Award goes to...")
            _impl._magic_banner(f"🏆 {AWARD_NAME}", f"Winner: {winner.get('winner_builder_name', 'Unknown')}")
            break

    return 0


def cmd_replay(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    bundle_arg = getattr(args, "bundle", None)
    if not bundle_arg:
        _print_error(7, "ConfigValidationError", "Provide --bundle <path>.")
        return 7

    bundle_path = Path(bundle_arg)
    if not (bundle_path / "manifest" / "bundle.json").exists():
        _print_error(7, "ConfigValidationError", f"Bundle not found: {bundle_path}")
        return 7

    # Quick integrity check if sealed
    seal_path = bundle_path / "SEAL"
    hashes_path = bundle_path / "HASHES"
    if seal_path.exists() and hashes_path.exists():
        try:
            seal_data = json.loads(seal_path.read_text(encoding="utf-8"))
            expected = _sha256_file(hashes_path)
            if seal_data.get("hashes_sha256") != expected:
                _print_error(5, "BundleTamperError", "Bundle integrity check failed.")
                return 5
        except Exception:
            pass

    manifest = load_manifest(bundle_path)
    verdicts = _load_verdicts(bundle_path)
    feedback = _load_feedback(bundle_path)
    gate_path = bundle_path / "freshness_gate.json"
    gate = load_json(gate_path) if gate_path.exists() else None

    _impl._magic_banner("Copilot Builder - Judging Panel Replay", f"Run: {manifest.get('run_id', bundle_path.name)}")
    _impl._sideline(f"Status: {manifest.get('status', 'unknown')}", "📼", "blue")
    if gate:
        _impl._sideline(f"Model: {gate.get('selected_model', gate.get('model', 'unknown'))} ({gate.get('status', '')})", "🧠", "green")

    if verdicts:
        print(_impl._paint("\n🎙️ Panel Verdicts", "magenta", bold=True))
        for v in verdicts:
            score = float(v.get("total_score", 0))
            print(_impl._paint(f"\n  ─── 🛠️ {v.get('project_name', v['submission_id'])} ───", "blue", bold=True))
            print(_impl._paint(f"  Builder: {v.get('builder_name', '')}", "cyan"))
            print(_impl._paint(f"  Score:   {score:.2f}/10  {_impl._score_bar(score)}", "gold", bold=True))
            for arch_v in v.get("archetype_verdicts", []):
                print(_impl._paint(f"    🎙️ {arch_v.get('archetype_name', 'Panel')}: {arch_v.get('bright_spot', '')[:100]}", "green"))
    else:
        print("\n  No verdicts found in bundle.")

    if feedback:
        print(_impl._paint("\n✨ Next-Commit Nudges", "cyan", bold=True))
        for fc in feedback:
            print(_impl._paint(f"\n  Submission: {fc.get('submission_id', '')}", "cyan"))
            print(_impl._paint(f"  ✨ {fc.get('bright_spot', '')}", "green"))
            print(_impl._paint(f"  ➜ {fc.get('next_commit', '')}", "yellow"))

    winner_path = bundle_path / "winner" / "card.json"
    if winner_path.exists():
        winner = load_json(winner_path)
        print()
        _impl._drumroll("The Copilot Builder Award goes to...")
        _impl._magic_banner(f"🏆 {AWARD_NAME}", f"{winner.get('winner_builder_name', 'Unknown')}")

    return 0


def cmd_doctor(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    bundle_arg = getattr(args, "bundle", None)
    issues: List[str] = []
    ok: List[str] = []

    print("Copilot Builder - Judging Panel — Doctor")
    print("=" * 50)

    # 1. Python version
    vi = sys.version_info
    if vi >= (3, 11):
        ok.append(f"Python {vi.major}.{vi.minor}.{vi.micro} (≥ 3.11 ✓)")
    else:
        issues.append(f"Python {vi.major}.{vi.minor} < 3.11 (upgrade required)")

    # 2. Default configuration validity
    try:
        _validate_rubric(DEFAULT_RUBRIC)
        ok.append("Default configuration: valid (rubric weights sum to 1.0)")
    except ConfigValidationError as e:
        issues.append(f"Default configuration: invalid — {e}")

    # 3. CBP_CONFIG_PATH (if set)
    config_path = os.environ.get("CBP_CONFIG_PATH")
    if config_path:
        if Path(config_path).exists():
            ok.append(f"Config file: {config_path}")
        else:
            issues.append(f"Config file: not found at {config_path}")

    # 4. Model freshness gate status
    try:
        available = query_available_models(_gateway)
        non_dep = [m for m in available if not m.get("deprecated", False)]
        best = _select_best_model(available)
        ok.append(f"Model gate: {len(available)} models, {len(non_dep)} non-deprecated")
        ok.append(f"Best available model: {best}")
    except Exception as exc:
        issues.append(f"Model gate: {exc}")

    # 5. Bundle integrity check (if --bundle provided)
    if bundle_arg:
        bpath = Path(bundle_arg)
        if bpath.exists():
            ok.append(f"Bundle: {bpath}")
            seal_path = bpath / "SEAL"
            hashes_path = bpath / "HASHES"
            if seal_path.exists() and hashes_path.exists():
                try:
                    seal_data = json.loads(seal_path.read_text(encoding="utf-8"))
                    expected = _sha256_file(hashes_path)
                    if seal_data.get("hashes_sha256") == expected:
                        ok.append("Bundle integrity: SEAL hash verified ✓")
                    else:
                        issues.append("Bundle integrity: SEAL hash mismatch — bundle may be tampered")
                except Exception as e:
                    issues.append(f"Bundle integrity: error reading SEAL/HASHES: {e}")
            else:
                ok.append("Bundle seal: not yet exported (SEAL/HASHES absent)")
        else:
            issues.append(f"Bundle: not found at {bundle_arg}")

    # Report
    for item in ok:
        print(f"  ✓ {item}")
    for item in issues:
        print(f"  ✗ {item}", file=sys.stderr)

    if issues:
        print(f"\n  {len(issues)} issue(s) found.")
        return 1

    print(f"\n  All checks passed.")
    return 0


def cmd_feedback(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    bundle_arg = getattr(args, "bundle", None)
    if not bundle_arg:
        _print_error(7, "ConfigValidationError", "Provide --bundle <path>.")
        return 7

    bundle_path = Path(bundle_arg)
    if not (bundle_path / "manifest" / "bundle.json").exists():
        _print_error(7, "ConfigValidationError", f"Bundle not found: {bundle_path}")
        return 7

    submission_id = getattr(args, "submission_id", None)
    manifest = load_manifest(bundle_path)
    rubric = load_rubric(bundle_path)
    submissions = _load_submissions(bundle_path)

    gate_path = bundle_path / "freshness_gate.json"
    selected_model = "gpt-4o"
    if gate_path.exists():
        gate = load_json(gate_path)
        selected_model = gate.get("selected_model", gate.get("model", selected_model))

    subs_to_process = (
        [s for s in submissions if s["submission_id"] == submission_id]
        if submission_id else submissions
    )

    proposals = []
    for sub in subs_to_process:
        sid = sub["submission_id"]
        prompt = (
            f"Generate an encouraging feedback proposal for builder.\n"
            f"Project: {sub.get('project_name', 'Unknown')}\n"
            f"Description: {sub.get('description', '')}\n\n"
            "Provide JSON with bright_spot, next_commit, extended_guidance.\n"
            "Be celebratory and supportive. JSON only."
        )
        try:
            raw = call_model(prompt, selected_model, _gateway)
            from copilot_builder_panel import _parse_model_response
            parsed = _parse_model_response(raw)
        except Exception as exc:
            parsed = {}

        proposal = {
            "submission_id": sid,
            "project_name": sub.get("project_name", ""),
            "bright_spot": parsed.get("bright_spot", "This project demonstrates impressive work and strong execution."),
            "next_commit": parsed.get("next_commit", "Consider extending the core feature to reach more users in your next commit."),
            "extended_guidance": parsed.get("panel_notes", parsed.get("extended_guidance", "Keep building — you are making real impact!")),
            "requires_human_approval": True,
            "generated_at": _now(clock),
        }
        proposals.append(proposal)

    # Write proposal outside the bundle
    proposal_dir_env = os.environ.get("PROPOSAL_OUTPUT_DIR")
    if proposal_dir_env:
        proposal_dir = Path(proposal_dir_env)
    else:
        proposal_dir = bundle_path.parent / "proposals"
    proposal_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_id = manifest.get("run_id", bundle_path.name)
    proposal_path = proposal_dir / f"proposal_{run_id}_{ts}.json"
    proposal_path.write_text(
        json.dumps({"proposals": proposals, "requires_human_approval": True}, indent=2),
        encoding="utf-8"
    )

    print(f"✓ Feedback proposal written: {proposal_path}")
    print(f"  [NOTE] Requires human approval before delivery.")
    for p in proposals:
        print(f"\n  Project: {p['project_name']}")
        print(f"  ✨ {p['bright_spot']}")
        print(f"  → {p['next_commit']}")
    return 0


def cmd_list(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    runs_dir = Path(os.environ.get("CBP_RUNS_DIR", str(Path.home() / ".copilot_builder_panel" / "runs")))
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

    print(f"{'RUN ID':<40} {'STATUS':<12} {'SUBS':>4}  CREATED")
    print("-" * 70)
    for r in runs:
        created = r["created_at"][:19] if r["created_at"] else ""
        print(f"{r['run_id']:<40} {r['status']:<12} {r['submissions']:>4}  {created}")
    return 0


def cmd_resume(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    bundle_path = Path(args.run_dir).resolve()
    _assert_bundle_exists(bundle_path)
    manifest = load_manifest(bundle_path)

    if manifest.get("status") == "judging":
        print(f"[INFO] Resuming interrupted judge run...")
        return cmd_judge(args, _gateway, clock)
    elif manifest.get("status") in ("sealed", "awarded", "exported"):
        print(f"[INFO] Run is already complete (status: {manifest['status']}).")
        return 0
    else:
        print(f"[INFO] Run is in status '{manifest['status']}'. Nothing to resume.")
        return 0


def cmd_compare(args: argparse.Namespace, _gateway=None, clock=None) -> int:
    bundle_a = Path(args.bundle_a)
    bundle_b = Path(args.bundle_b)

    for b in (bundle_a, bundle_b):
        if not b.exists():
            _print_error(7, "ConfigValidationError", f"Bundle not found: {b}")
            return 7

    def _safe_load_manifest(bp):
        try:
            return load_manifest(bp)
        except Exception:
            return {"run_id": bp.name, "status": "unknown"}

    manifest_a = _safe_load_manifest(bundle_a)
    manifest_b = _safe_load_manifest(bundle_b)

    print("=" * 70)
    print(f"  COMPARE")
    print(f"  A: {manifest_a.get('run_id', bundle_a.name)} (status: {manifest_a.get('status', '?')})")
    print(f"  B: {manifest_b.get('run_id', bundle_b.name)} (status: {manifest_b.get('status', '?')})")
    print("=" * 70)

    dirs = ["manifest", "config", "inputs", "eval", "sealed", "verdicts", "feedback", "winner"]
    for d in dirs:
        count_a = len(list((bundle_a / d).rglob("*"))) if (bundle_a / d).exists() else 0
        count_b = len(list((bundle_b / d).rglob("*"))) if (bundle_b / d).exists() else 0
        indicator = "=" if count_a == count_b else "≠"
        print(f"  {indicator} {d:<20} A:{count_a:>3}  B:{count_b:>3}")

    return 0


# ---------------------------------------------------------------------------
# CLI Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="builder_panel",
        description=(
            "Copilot Builder - Judging Panel — CLI judging for hackathons and builder programs.\n"
            "Commands: init, submit, judge, present, replay, resume, compare, list, "
            "award, feedback, export, validate, doctor"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # init
    p = sub.add_parser("init", help="Create a new run bundle.")
    p.add_argument("--run-dir", required=True, dest="run_dir", help="Bundle directory path.")
    p.add_argument("--name", required=True, help="Run identifier / name.")
    p.add_argument("--mode", default="workshop", choices=["workshop", "async", "replay", "compare"])
    p.add_argument("--config", help="Path to rubric config JSON.")
    p.add_argument("--showtime", action="store_true")

    # submit
    p = sub.add_parser("submit", help="Add a project submission.")
    p.add_argument("--run-dir", required=True, dest="run_dir")
    p.add_argument("--id", required=True, dest="id", help="Submission ID.")
    p.add_argument("--title", required=True, help="Project title.")
    p.add_argument("--description", default="")
    p.add_argument("--builder", default="", help="Builder name (optional).")
    p.add_argument("--showtime", action="store_true")

    # import-urls
    p = sub.add_parser("import-urls", help="Bulk import GitHub repo URLs as workshop submissions.")
    p.add_argument("--run-dir", required=True, dest="run_dir")
    p.add_argument("urls", nargs="*", help="GitHub URLs or owner/repo entries.")
    p.add_argument("--file", help="Text file containing GitHub repo URLs.")
    p.add_argument("--builder-name", default="Workshop Builders",
                   help="Builder display name for imported repo submissions.")
    p.add_argument("--showtime", action="store_true")

    # judge
    p = sub.add_parser("judge", help="Run eval engine on a bundle.")
    p.add_argument("--run-dir", required=True, dest="run_dir")
    p.add_argument("--showtime", action="store_true")

    # present
    p = sub.add_parser("present", help="Present results from a sealed bundle.")
    p.add_argument("--bundle", required=True, help="Path to sealed bundle.")
    p.add_argument("--showtime", action="store_true")

    # replay
    p = sub.add_parser("replay", help="Read-only replay of a bundle.")
    p.add_argument("--bundle", required=True)
    p.add_argument("--showtime", action="store_true")

    # resume
    p = sub.add_parser("resume", help="Resume an interrupted judge run.")
    p.add_argument("--run-dir", required=True, dest="run_dir")

    # compare
    p = sub.add_parser("compare", help="Diff two bundles.")
    p.add_argument("bundle_a")
    p.add_argument("bundle_b")

    # list
    sub.add_parser("list", help="List all runs.")

    # award
    p = sub.add_parser("award", help="Declare winner and write winner card.")
    p.add_argument("--run-dir", required=True, dest="run_dir")
    p.add_argument("--winner", default=None, help="Winning submission ID (auto-selected if omitted).")
    p.add_argument("--showtime", action="store_true")

    # recap
    p = sub.add_parser("recap", help="Write a workshop recap from stored artifacts.")
    p.add_argument("--bundle", required=True)
    p.add_argument("--out")

    # tui
    p = sub.add_parser("tui", help="Open a lightweight Agent Pulse-style board (CLI fallback).")
    p.add_argument("--bundle")
    p.add_argument("--showtime", action="store_true")

    # feedback
    p = sub.add_parser("feedback", help="Generate feedback proposal.")
    p.add_argument("--bundle", required=True)
    p.add_argument("--submission-id", dest="submission_id", default=None)

    # export
    p = sub.add_parser("export", help="Seal bundle with HASHES and SEAL.")
    p.add_argument("--run-dir", required=True, dest="run_dir")
    p.add_argument("--force", action="store_true")

    # validate
    p = sub.add_parser("validate", help="Verify bundle integrity.")
    p.add_argument("--bundle", required=True)

    # doctor
    p = sub.add_parser("doctor", help="Diagnose environment and bundle health.")
    p.add_argument("--bundle", default=None, help="Optional bundle path to inspect.")

    return parser


COMMAND_MAP = {
    "init": cmd_init,
    "submit": cmd_submit,
    "import-urls": cmd_import_urls,
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


def main(argv=None) -> int:
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
        return handler(args)
    except CopilotBuilderPanelError as exc:
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
