# Hackathon Judge Agent Guide

## Canonical surfaces

- Use `hackathon_judge.py` for the CLI.
- Use `hackathon_judge_dashboard.py` only for the optional Textual run monitor.
- Use `HJ_RUNS_DIR`, `HJ_REGISTRY_PATH`, `HJ_COLOR`, `HJ_NO_COLOR`, and
  `HJ_SHOWTIME` for local configuration.
- Treat `config/event.example.json` as the supported starting point for a new
  event.

## Product invariants

- Keep the default experience general-purpose: no host personality or
  organization branding. Default Live Show uses a bronze → silver → gold
  podium; custom EventSpecs may define an alternate award slate.
- Keep one primary audience surface: the current Live Show terminal contains
  the run of show and commentary. Never auto-open a dashboard or second terminal.
- Live Show commentary may use a concise generic sideline-reporter voice and
  startup demo-day energy, but never a named personality or publication imitation.
- Before the final result, choose one audience-participation cue, wait for
  operator confirmation in an interactive show, then reveal.
- Keep the bundled practice flow within the 120-second show budget.
- Audience views must not reveal numeric scores, rankings, prompts, or awards
  before the event status is `awarded` or `exported`.
- Only assess Copilot use or frontier use from explicit builder-provided
  evidence. When no evidence is supplied, say so; never infer either claim
  from a project link, repository metadata, or model impression.
- Keep Quick and Slack judging quiet and operator-facing. Fun emcee commentary,
  countdowns, and shared-screen ceremony are Live Show-only behavior.
- Default public scoring must use the configured premium multi-model panel and
  median consensus; strict events must block rather than silently lose a panel
  member or provider family.
- Exact consensus ties must follow the event's explicit policy: shared podium
  by default, a predeclared sealed tiebreaker, or a logged human resolution.
  Never let submission arrival or filename order decide a podium place.
- Award selection must use the tie policy sealed with scoring and reject a
  later mismatch in the event snapshot.
- Keep the Shadow Spec sealed until awards and diagnostic-only. It may flag
  quality risks, calibration issues, or leakage, but it must never change
  public scores, rankings, or awards.
- Feedback may suggest optional Copilot next moves and frontier experiments,
  but it must not turn those suggestions into claims that a project used either
  capability.
- Preserve source labels for builder-provided project context. Project-specific
  feedback must use that context or clearly label an unsupported suggestion as
  a hypothesis; audience projections must withhold raw context and redact any
  score, rank, winner, or award language before awards. Safe grounded project
  summaries may remain specific.
- Keep live panel calls bounded by the configured concurrency limit. A live
  time budget is warn-only unless a future EventSpec policy explicitly defines
  another safe behavior; it must never silently reduce a strict panel.
- Live progress telemetry may expose only aggregate stage, project-count, call
  count, and ETA data. It must never include a project result, score, rank, or
  model response in an audience projection.
- Treat simulated evaluations as practice demos, never as official award
  outcomes.
- Make the project the primary subject of every spotlight and award card.
  Treat team attribution as supporting context, and showcase only non-scoring
  repository context plus explicit builder-provided evidence.
- Preserve historic rubric-only bundle readability.
- Replay must use stored artifacts only and never call a model.
- Do not weaken write-once artifacts, hash seals, run-ID validation, or safe
  archive extraction.

## Security

- Never add credentials, tokens, private run bundles, or confidential project
  metadata to the repository.
- Treat generated run bundles as internal artifacts.
- Keep CodeQL, Dependabot, and secret-scanning configuration intact.
- Do not use `--force` or re-seal an exported bundle.

## Validation

Run the focused suite after Python changes:

```bash
python3 -m pytest -q
python3 -m py_compile hackathon_judge.py hackathon_judge_dashboard.py event_spec.py bundle_reader.py
```
