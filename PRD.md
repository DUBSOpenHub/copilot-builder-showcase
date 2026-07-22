# Hackathon Judge
## Product Requirements Document

## Purpose

Hackathon Judge runs a clear, fair, and celebratory project judging experience
from a terminal. An organizer types `hackathon`, pastes project links,
screen-shares a single guided show, and leaves with a replayable,
tamper-evident event bundle.

The product is event-neutral: it does not impose a named host personality,
organization identity, or judge character. The default show uses a bronze →
silver → gold podium; EventSpecs may replace that award slate.

## Primary users

| User | Need |
|---|---|
| Facilitator | Start a credible judging show with one command |
| Judge | Review project links through a clear rubric without score leaks |
| Participant | Receive a spotlight and useful feedback |
| Audience | Follow visible progress and celebrate a fair reveal |
| Operator | Access post-award scores and replay evidence |

## Success criteria

1. A first-time facilitator can type `hackathon`, paste project links, and
   start without knowing Python, subcommands, panel internals, or EventSpecs.
2. An audience view never shows numerical totals, dimension scores, rank order,
   judge prompts, or unrevealed awards before the award stage.
3. Every project receives a spotlight before the celebration.
4. A saved bundle can be validated and replayed without a model call.
5. A historical rubric-only bundle remains readable.
6. The primary experience uses one visible terminal and never auto-opens a
   second audience or monitor window.
7. The bundled practice show completes the full intake-to-replay flow within
   120 seconds under supported local conditions.
8. The final result follows one randomly selected audience-participation cue and
   explicit operator confirmation in an interactive Live Show.
9. `PRACTICE SHOW — ILLUSTRATIVE RESULTS` or `OFFICIAL LIVE PANEL` remains
   visible in the title, opening, run card, act breaks, receipt, and manifest.
10. Installation creates the primary `hackathon` command, preserves the
    advanced `hackathon-judge` CLI, and never blocks the Live Show when the
    optional monitor dependency is unavailable.

## Core flow

```text
type hackathon and paste project links
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
audience participation and confirmation
        |
        v
award reveal and operator score access
        |
        v
recap, validation, and immutable replay archive
```

The intended first-run command is:

```bash
hackathon
```

The command collects one project link per line and begins when the organizer
submits an empty line. Links can also be supplied directly:

```bash
hackathon owner/project-one owner/project-two
```

The complete run of show and commentary appears in one terminal. The current
Practice or Official result status remains visible throughout.

## EventSpec

An EventSpec is a portable JSON document that is validated and snapshotted at
run initialization. It includes:

```text
event                name and tagline
rubric               weighted scoring dimensions
review_lenses        neutral evidence-focused perspectives
awards               recognition categories and selection dimensions
presentation         audience-view and optional-monitor defaults
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

The optional Textual monitor must use the audience projection by default. It is
never auto-launched by the Live Show. Any operator projection is explicit and
remains unavailable before awards.

## Reliability and integrity

- Project imports are idempotent and preserve GitHub metadata when available.
- All initial EventSpec, rubric, input, evaluation, verdict, and award artifacts
  are write-once or append-only as appropriate.
- `freshness_gate.json` records the selected judges and whether the run used an
  Official Live Panel or illustrative practice judges.
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
- Automatic launch of a second audience window

## Compatibility

`hackathon_launcher.py` is the beginner entry point and routes the default
experience into `hackathon_judge.py`, the canonical implementation. The
installer preserves `hackathon-judge` for advanced compatibility. Existing
bundles with only `config/rubric.json` are read through a legacy adapter; they
do not need to be rewritten to use the current audience and replay surfaces.
