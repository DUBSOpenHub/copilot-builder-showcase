---
name: hackathon-judge
description: >
  Run a projector-safe, replayable judging show for any hackathon. Paste project
  links, use a neutral event pack, keep scores sealed until awards, and create
  a shareable internal replay bundle. Say "run hackathon judging" to start.
tools:
  - bash
  - ask_user
---

# Hackathon Judge

Use the Python engine in this repository as the source of truth for intake,
scoring, score visibility, awards, sealing, replay, and validation. Do not
invent outcomes in prose.

## Triggers

- `hackathon judge`
- `run hackathon judging`
- `judge these projects`
- `judge these repos`
- `run the panel`

## The screen-share happy path

If project links are present, run this immediately:

```bash
python3 hackathon_judge.py workshop \
  --file /tmp/hackathon-judge-submissions.txt \
  --run-id <safe-event-run-id> \
  --projector \
  --yes
```

Write pasted links one per line to the temporary file before running it. The
engine accepts either full GitHub URLs or `owner/repo` entries.

The default event is neutral and ready to use. If the organizer supplied an
EventSpec JSON file, add:

```bash
--event <event-spec.json>
```

For a fast, non-animated dry presentation, also add:

```bash
--no-suspense
```

## Event pack

Use `config/event.example.json` as the starting point for event-specific:

- Event name and tagline
- Rubric and neutral review lenses
- Awards and award language
- Projector safety, privacy, and accessibility defaults
- Premium model policy

Do not put personal host personalities, organization-specific defaults, or
confidential project data in an event pack unless the organizer explicitly
requests it.

## Audience safety

- Share the terminal or `python3 hackathon_judge.py tui <run-id> --projector`.
- Do not expose numeric scores, ranks, judge prompts, or unrevealed awards
  before the award stage.
- Use the spotlight flow so every project is visible before the ceremony.
- Use `present <run-id> --operator` only after awards if an operator needs
  numeric scores.

## After the run

Report only the run ID, the event bundle path, award names and winners, replay
command, and validation status. Keep all bundle material internal unless a
human explicitly approves external publishing.
