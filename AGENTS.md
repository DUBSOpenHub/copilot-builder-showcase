# Hackathon Judge Agent Guide

## Canonical surfaces

- Use `hackathon_judge.py` for the CLI.
- Use `hackathon_judge_dashboard.py` for the Textual projector board.
- Use `HJ_RUNS_DIR`, `HJ_REGISTRY_PATH`, `HJ_COLOR`, `HJ_NO_COLOR`, and
  `HJ_SHOWTIME` for local configuration.
- Treat `config/event.example.json` as the supported starting point for a new
  event.

## Product invariants

- Keep the default experience neutral: no host personality, organization
  branding, or fixed award slate.
- Audience views must not reveal numeric scores, rankings, prompts, or awards
  before the event status is `awarded` or `exported`.
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
