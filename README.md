# Copilot Builder Panel

CLI-native judging for hackathons, builder programs, and product workshops.  
Builders submit projects; a fictional panel scores them; standout projects earn the **Copilot Builder Award**.

> **Private testing repo:** This project is currently private while the workshop experience is being tested and polished.

---

## Quick Start

```bash
# Live workshop flow: paste repos, spotlight every builder, reveal three awards
python3 copilot_builder_panel.py workshop --file submissions.txt

# 1. Create a run
python3 copilot_builder_panel.py init my-workshop-2026

# 2. Add submissions
python3 copilot_builder_panel.py submit my-workshop-2026 \
  --builder-name "Alice Chen" \
  --project-name "Copilot Code Compass" \
  --description "AI-powered navigation for legacy codebases"

# Or paste a whole room of GitHub repos at once
python3 copilot_builder_panel.py import-urls my-workshop-2026 \
  https://github.com/octocat/Hello-World \
  DUBSOpenHub/terminal-stampede \
  DUBSOpenHub/copilot-cli-agent-pulse

# 3. Judge with workshop pacing
python3 copilot_builder_panel.py judge my-workshop-2026 --showtime

# 4. Present
python3 copilot_builder_panel.py present my-workshop-2026 --showtime

# 5. Award the winner
python3 copilot_builder_panel.py award my-workshop-2026 --winner <submission_id> --showtime

# 6. Export a recap and immutable bundle
python3 copilot_builder_panel.py recap my-workshop-2026
python3 copilot_builder_panel.py export my-workshop-2026

# Optional: open the lightweight live board
python3 copilot_builder_panel.py tui my-workshop-2026 --showtime
```

---

## All Commands

### `workshop`
Live facilitator flow. By default it keeps the room moving: paste repo URLs, watch projects enter, let premium judges score, spotlight every builder, reveal three awards, and finish with a sealed recap.

```bash
python3 copilot_builder_panel.py workshop
python3 copilot_builder_panel.py workshop --file submissions.txt
python3 copilot_builder_panel.py workshop --yes --file submissions.txt --run-id demo-room
python3 copilot_builder_panel.py workshop --file submissions.txt --no-suspense
python3 copilot_builder_panel.py workshop --configure --manual-confirm
```

Default award slate:
- **Copilot Builder Award**
- **Copilot Spark Award**
- **Copilot Ship Award**

`--configure` brings back advanced setup questions. `--manual-confirm` asks before each stage; otherwise workshop mode is designed as a one-paste live show. `--no-suspense` keeps the same output without live countdown pauses for fast demos or CI.

---

## Awards

The panel keeps awards intentionally focused:

| Award | Use |
|---|---|
| **Copilot Spark Award** | Most original or creative idea |
| **Copilot Ship Award** | Most demo-ready or shippable project |
| **Copilot Builder Award** | Best overall project |

The workshop flow writes all three awards into the sealed run bundle and replay output.

### `init`
Create a new named run with rubric config.

```bash
python3 copilot_builder_panel.py init <run_id> [--mode workshop|async] [--config rubric.json]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--mode` | `workshop` | Run mode: `workshop`, `async`, `replay`, `compare` |
| `--config` | built-in | Path to custom rubric JSON config |

---

### `submit`
Add a project submission to a run.

```bash
python3 copilot_builder_panel.py submit <run_id> \
  --builder-name "Name" \
  --project-name "Project" \
  --description "Description" \
  [--file path/to/file]
```

| Option | Required | Description |
|--------|----------|-------------|
| `--builder-name` | Yes | Builder's display name |
| `--project-name` | Yes | Project name |
| `--description` | No | Project description |
| `--file` | No | Attach a file (may repeat) |

---

### `import-urls`
Bulk-import a workshop room full of GitHub repository URLs or `owner/repo` entries.

```bash
python3 copilot_builder_panel.py import-urls <run_id> <url-or-owner/repo>...
python3 copilot_builder_panel.py import-urls <run_id> --file submissions.txt
cat submissions.txt | python3 copilot_builder_panel.py import-urls <run_id>
```

This command deduplicates repositories and turns each URL into a submission.
If the `gh` CLI is authenticated, it enriches each submission with public repository metadata.

---

### `judge`
Trigger the eval engine. Runs the freshness gate, scores all submissions, seals the shadow score, builds panel verdicts and feedback cards.

```bash
python3 copilot_builder_panel.py judge <run_id>
python3 copilot_builder_panel.py judge <run_id> --showtime
```

**Effects:**
- Writes `freshness_gate.json` (write-once). Default policy prefers premium high-reasoning models.
- Writes `eval/step_*.json` per submission
- Seals `sealed/shadow_score.json` (write-once, read-only after seal)
- Writes `verdicts/<submission_id>.json`
- Writes `feedback/<submission_id>.json`

---

### `present`
Generate presentation from stored artifacts. **Zero live model calls.**  
Two consecutive calls produce identical output (AC-07).

```bash
python3 copilot_builder_panel.py present <run_id>
python3 copilot_builder_panel.py present <run_id> --showtime
```

---

### `replay`
Read-only replay of any prior bundle. Validates integrity first. **No new artifacts, no model calls.**

```bash
python3 copilot_builder_panel.py replay <run_id>
python3 copilot_builder_panel.py replay /path/to/bundle/dir
python3 copilot_builder_panel.py replay /path/to/run.bundle.tar.gz
python3 copilot_builder_panel.py replay <run_id> --showtime
```

---

### `resume`
Re-enter an interrupted `judge` run at the last completed eval step.

```bash
python3 copilot_builder_panel.py resume <run_id>
```

---

### `compare`
Side-by-side diff of two sealed run bundles. Outputs to stdout only.

```bash
python3 copilot_builder_panel.py compare <run_id_a> <run_id_b>
python3 copilot_builder_panel.py compare /path/to/bundle_a /path/to/bundle_b
```

---

### `list`
Enumerate all runs and their statuses.

```bash
python3 copilot_builder_panel.py list
```

---

### `award`
Declare the winner. Writes a winner card (requires human approval) and appends a registry entry.

```bash
python3 copilot_builder_panel.py award <run_id> --winner <submission_id>
python3 copilot_builder_panel.py award <run_id> --winner <submission_id> --showtime
```

Winner card always contains `"requires_human_approval": true`.  
Registry is append-only; prior entries are never modified.

---

### `recap`
Write a workshop recap from stored artifacts only.

```bash
python3 copilot_builder_panel.py recap <run_id>
python3 copilot_builder_panel.py recap <run_id> --out workshop-recap.md
```

The recap includes submissions, scores, bright spots, next-commit nudges, and the winner if one has been awarded.

---

### `tui`
Open a lightweight Agent Pulse-style board. If a run is provided, it presents that run from stored artifacts. If no run is provided, it shows the run list.

```bash
python3 copilot_builder_panel.py tui
python3 copilot_builder_panel.py tui <run_id> --showtime
```

The MVP uses a colorful CLI fallback; a Textual dashboard can be layered on later without changing bundle artifacts.

---

### `feedback`
Generate an enhanced feedback proposal. **Does not modify any existing bundle artifact.**  
Human approval is required before delivering.

```bash
python3 copilot_builder_panel.py feedback <run_id>
python3 copilot_builder_panel.py feedback <run_id> --submission-id <id>
```

---

### `export`
Package the full immutable bundle. Writes `HASHES`, `SEAL`, and a `.tar.gz` archive.

```bash
python3 copilot_builder_panel.py export <run_id>
python3 copilot_builder_panel.py export <run_id> --force  # re-seal if already exported
```

**Bundle seal order:**
1. Update manifest status → `exported`
2. Compute SHA-256 of every artifact → write `HASHES` (write-once)
3. Compute SHA-256 of `HASHES` → write `SEAL` (write-once)
4. Create `<run_id>.bundle.tar.gz`

---

### `validate`
Verify `HASHES` and `SEAL` integrity. Completes in <5s for any bundle.

```bash
python3 copilot_builder_panel.py validate <run_id>
python3 copilot_builder_panel.py validate /path/to/bundle
```

**Exit codes:** `0` = pass, `5` = tampered/missing files.

---

### `doctor`
Diagnose config, model gate, and optional bundle health. Does not modify state.

```bash
python3 copilot_builder_panel.py doctor
python3 copilot_builder_panel.py doctor <run_id>
```

---

## Exit Codes

| Code | Class | Meaning |
|------|-------|---------|
| 0 | — | Success |
| 1 | `UnhandledException` | Unexpected error |
| 2 | `BundleSealError` | Write-once violation |
| 3 | `FreshnessGateBlock` | Stale model in strict mode |
| 4 | `ToneSafetyFailure` | Banned phrase or missing required element |
| 5 | `BundleTamperError` | Hash mismatch in validate |
| 6 | `SubmissionSizeError` | Input exceeds configured cap |
| 7 | `ConfigValidationError` | Rubric or config error |
| 8 | `ModelAPIError` | API call failure |
| 9 | `HumanApprovalGate` | Export blocked before winner approval |

---

## Bundle Structure

```
<run_id>/
├── manifest/bundle.json          # run_id, status, command_log[]
├── config/rubric.json            # snapshotted at init; immutable after judge
├── inputs/<submission_id>.json   # write-once per submission
├── eval/step_<n>.json            # one per scoring pass; append-only
├── freshness_gate.json           # written once before eval
├── sealed/shadow_score.json      # write-once; chmod 0o444 after seal
├── verdicts/<submission_id>.json # per-submission panel verdict
├── feedback/<submission_id>.json # per-submission feedback card
├── winner/card.json              # { requires_human_approval: true }
├── registry/log.ndjson           # append-only winner registry
├── HASHES                        # SHA-256 of every artifact (written at export)
└── SEAL                          # SHA-256 of HASHES (written at export)
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CBP_RUNS_DIR` | `~/.copilot_builder_panel/runs` | Root directory for all run bundles |
| `CBP_REGISTRY_PATH` | `~/.copilot_builder_panel/registry/log.ndjson` | Global winner registry path |
| `CBP_COLOR` | auto | Set to `always` to force ANSI color in captured demos |
| `CBP_SHOWTIME` | off | Set to `1` to add workshop pacing without passing `--showtime` |

---

## Configuration (`config/rubric.json`)

```json
{
  "version": "1.0",
  "rubric": {
    "dimensions": [
      { "id": "innovation",   "name": "Innovation",          "weight": 0.30, "max_score": 10 },
      { "id": "impact",       "name": "Potential Impact",    "weight": 0.25, "max_score": 10 },
      { "id": "execution",    "name": "Technical Execution", "weight": 0.25, "max_score": 10 },
      { "id": "presentation", "name": "Clarity & Demo",      "weight": 0.20, "max_score": 10 }
    ]
  },
  "judge_archetypes": [
    { "id": "spark",    "name": "The Spark",    "focus": "novel ideas and creative leaps" },
    { "id": "builder",  "name": "The Builder",  "focus": "technical depth and craft" },
    { "id": "champion", "name": "The Champion", "focus": "real-world impact and adoption" }
  ],
  "tone_policy": {
    "banned_phrases": [],
    "extra_banned_phrases": []
  },
  "freshness_gate": {
    "policy_mode": "strict",
    "preferred_model": "claude-opus-4.7-high",
    "required_tier": "premium",
    "required_reasoning": "high"
  },
  "submission_size_cap_bytes": 5242880
}
```

Dimension weights **must sum to 1.0**. Judge archetypes must be fictional (no real-person names).

---

## Running Tests

```bash
python3 -m pytest tests/test_copilot_builder_panel.py -v
```

All 83 tests cover: tone safety (DF-07), write-once integrity, shadow score vault, freshness gate, full e2e flows (AC-01–AC-10), exit codes, config validation, and replay fidelity.

---

## Requirements

- Python 3.11+
- No mandatory dependencies beyond the standard library
- Optional: model API client (injected via `_gateway` parameter; falls back to deterministic synthetic responses)

---

## 🔒 Security

This repository is private during the testing phase. See [SECURITY.md](SECURITY.md) for reporting guidance.

Security baseline:

- GitHub Actions CI for compile + tests
- CodeQL workflow prepared for Python analysis; alert upload requires code scanning to be enabled for this private repo
- Dependabot for GitHub Actions updates
- CODEOWNERS requiring `@DUBSOpenHub` ownership
- Generated run bundles ignored by git

---

🐙 Created with 💜 by [@DUBSOpenHub](https://github.com/DUBSOpenHub) with the [GitHub Copilot CLI](https://docs.github.com/copilot/concepts/agents/about-copilot-cli).

Let's build! 🚀✨
