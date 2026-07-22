---
name: hackathon-judge
description: >
  Turn bulk project links into one single-screen, replayable Hackathon Judge
  Live Show. Every project gets a spotlight, scores stay sealed, the audience
  joins one suspenseful final-reveal moment, and the run is preserved.
  Say "run hackathon judging" to start.
tools:
  - bash
  - ask_user
  - slack
---

# Hackathon Judge

Use `hackathon_judge.py` as the source of truth for intake, judging, score
visibility, awards, sealing, replay, and validation. Never invent an outcome in
prose.

## Triggers

- `hackathon judge`
- `run hackathon judging`
- `judge these projects`
- `judge these repos`
- `run the panel`
- `run a hackathon judge demo`

## One experience

Hackathon Judge has one primary user experience: the **Live Show**.

- Do not offer Live, Quick, or Slack judging as mode choices.
- If project links or an uploaded submissions file are already present, start
  the Live Show immediately.
- If no project links are present, ask only for the links.
- If the organizer says `run again` or `start over`, reuse the previous project
  entries with a fresh run ID.
- If the organizer asks for a demo without providing links, run the bundled
  practice demo with `--demo`.

Quick judging and Slack intake may remain internal utilities, but do not surface
them as alternate product experiences.

## Project-entry format

Accept plain GitHub URLs or `owner/repo` entries. For team attribution and richer
feedback, prefer one entry per line:

```text
https://github.com/example/project | Team Aurora | Used Copilot Chat to design the API contract | Built an agent workflow with retrieval | Reduce missed follow-ups | Account executives | https://demo.example/aurora | Daily workflow demo
```

The optional fields after the URL are: `team or builder`, `Copilot evidence`,
`frontier evidence`, `problem statement`, `intended user`, `demo or artifact`,
and `builder notes`.

Never infer Copilot or frontier use from a link, code, repository metadata, or a
model impression. Missing evidence must remain `not provided`.

## Start the Live Show

Write supplied entries to a temporary file, then open exactly one real terminal:

```bash
python3 hackathon_judge.py workshop \
  --file <temporary-submissions-file> \
  --run-id <safe-event-run-id> \
  --require-live-terminal \
  --yes
```

On macOS:

```bash
osascript \
  -e 'tell application "Terminal" to do script "cd <hackathon-judge-repo> && python3 hackathon_judge.py workshop --file <temporary-submissions-file> --run-id <safe-event-run-id> --require-live-terminal --yes"' \
  -e 'tell application "Terminal" to activate'
```

The terminal labeled **LIVE SHOW — SHARE THIS WINDOW** contains the complete run
of show and commentary. Share that one window. Never auto-open the Textual
monitor or a second Terminal.

If no real terminal can be opened, stop before judging and provide the exact
manual command. Captured tool output is not the audience experience.

## Run the two-minute practice demo

Use the same Live Show, not a separate experience:

```bash
python3 hackathon_judge.py workshop \
  --demo \
  --run-id <safe-demo-run-id> \
  --require-live-terminal \
  --yes
```

The bundled demo is deterministic, avoids network metadata calls, exercises the
full intake-to-replay flow, and targets completion within 120 seconds when the
operator promptly confirms the audience cue. Use `--no-suspense` only for
unattended smoke automation. It must be described as an illustrative practice
demo, never an official competition result.

## Show direction

The Live Show should feel like a punchy startup demo day:

1. Project links enter immediately.
2. A generic sideline reporter describes the action with short, energetic lines.
3. Every project receives a data-rich spotlight and a specific panel reaction.
4. Scores, ranks, prompts, and awards stay sealed.
5. Before the final result, select one of the ten audience-participation cues,
   ask the operator to confirm the room is participating, then reveal.
6. Finish with a concise moment of joy, recap, export, validation, and replay.

Use suspense without adding a named host personality or imitating a specific
publication. Keep commentary event-neutral and concise enough for a two-minute
demo.

## Awards and ties

The default reveal is a project-first bronze → silver → gold podium. A supplied
EventSpec may define custom awards.

Exact ties follow the EventSpec policy:

- `shared-podium` is the default.
- `sealed-tiebreaker` uses predeclared rubric dimensions.
- `human-resolution` requires
  `--tie-resolution rank:<place>=<submission-id>`.

Never use entry order to break a tie. If a required human decision is missing,
stop at the award stage.

## Audience safety

- Use only the single Live Show terminal for the audience.
- Never expose numeric scores, ranks, judge prompts, unrevealed awards, or the
  sealed Shadow Spec before awards.
- Every accepted project must appear before the award ceremony.
- The optional `tui` command is a monitor for diagnostics only; it is not part of
  the primary show and must never auto-launch.
- Use `present <run-id> --operator` only after awards when numeric scores are
  needed privately.

## Event pack and feedback

Use `config/event.example.json` for event name, rubric, review lenses, awards,
privacy, accessibility, model policy, and tone policy. Do not add personal host
branding or confidential defaults.

After the show, private feedback may include award rationale, what judges liked,
one actionable next step, a Copilot next move, a bounded frontier experiment,
and explicit evidence status. Project-specific feedback must cite supplied
context; unsupported suggestions must be labeled hypotheses.

## After the run

Report the run ID, bundle path, private feedback proposal path, award names and
winners, replay command, and validation status. Keep run bundles internal unless
a human approves external publishing.
