# Hackathon Judge
## Product Requirements Document

## Purpose

Hackathon Judge runs a clear, fair, and celebratory project judging
experience from a terminal. An organizer pastes project links, screen-shares a
guided show, and leaves with a replayable, tamper-evident event bundle.

The product is event-neutral: it does not impose a host personality, organization
identity, judge character, or fixed award names.

## Primary users

| User | Need |
|---|---|
| Facilitator | Start a credible judging show with one command |
| Judge | Review project links through a clear rubric without score leaks |
| Participant | Receive a spotlight and useful feedback |
| Audience | Follow visible progress and celebrate a fair reveal |
| Operator | Access post-award scores and replay evidence |

## Success criteria

1. A facilitator can create a live event from project links and a JSON event
   pack without editing Python.
2. An audience view never shows numerical totals, dimension scores, rank order,
   judge prompts, or unrevealed awards before the award stage.
3. Every project receives a spotlight before the celebration.
4. A saved bundle can be validated and replayed without a model call.
5. A historical rubric-only bundle remains readable.

## Core flow

```text
paste project links
        |
        v
resolve and snapshot EventSpec
        |
        v
metadata-enriched project intake
        |
        v
premium policy check and sealed evaluation
        |
        v
audience-safe project spotlights
        |
        v
award reveal and operator score access
        |
        v
recap, validation, and immutable replay archive
```

The intended facilitator command is:

```bash
python3 hackathon_judge.py workshop \
  --event event.json \
  --file submissions.txt \
  --run-id event-2026 \
  --projector \
  --yes
```

## EventSpec

An EventSpec is a portable JSON document that is validated and snapshotted at
run initialization. It includes:

```text
event                name and tagline
rubric               weighted scoring dimensions
review_lenses        neutral evidence-focused perspectives
awards               recognition categories and selection dimensions
presentation         projector and audience-view defaults
privacy              score-visibility and internal-use defaults
accessibility        high contrast and reduced-motion defaults
model_policy         freshness, premium tier, and reasoning requirements
tone_policy          optional additional safety terms
```

`config/event.example.json` is the supported starting point. Legacy
`rubric.json` input remains supported and is adapted into a neutral EventSpec
at initialization.

## Score visibility

| Stage | Audience | Operator |
|---|---|---|
| Intake | Project identity and progress only | Same |
| Judging | Review progress only | Stored artifacts only |
| Spotlight before awards | Feedback and project highlights | Same |
| Award reveal | Winners and configured celebration | Same |
| After awards | Award results | `present --operator` can show numeric scores |

The Textual dashboard must use the audience projection by default. Any
operator projection is explicit and remains unavailable before awards.

## Reliability and integrity

- Project imports are idempotent and preserve GitHub metadata when available.
- All initial EventSpec, rubric, input, evaluation, verdict, and award artifacts
  are write-once or append-only as appropriate.
- `freshness_gate.json` records both the selected model and whether the
  evaluation was `live` or `simulated`.
- `HASHES` and `SEAL` bind exported artifacts to a replayable bundle.
- A sealed bundle cannot be force re-sealed.
- Replay archives reject paths, symlinks, hardlinks, and device entries that
  could escape the extraction directory.
- Run IDs are validated before any path is formed under the runs directory.

## Non-goals

- Public scoreboards before the award reveal
- Mandatory live model calls during replay
- Executable presentation templates
- Automatic external publication of winner material
- A second, divergent bundle or seal implementation

## Compatibility

`hackathon_judge.py` is the canonical implementation. Existing bundles with
only `config/rubric.json` are read through a legacy adapter; they do not need
to be rewritten to use the current audience and replay surfaces.
