# Copilot Builder Showcase
## Product Requirements Document

## Purpose

Copilot Builder Showcase gives workshops, product demos, conference build
sessions, and online challenges a shared resolution. A host types `showcase`,
pastes the projects people built, and creates a live Copilot judging showcase with
consistent reviews, one spotlight per output, audience participation, awards,
feedback, and a replayable event bundle.

The product turns any list of session outputs into a lightweight Copilot-judged
finale without requiring the host to recruit judges or produce a ceremony by
hand.

The product is event-neutral: it does not impose a named host personality,
organization identity, or judge character. The default showcase gives every project
a brief three-lens review, then awards Builder Bronze, Builder Silver, and the
first-place Copilot Builder Award. EventSpecs may replace that podium.

## Primary users

| User | Need |
|---|---|
| Facilitator | Give a builder session a watchable shared ending with one command |
| Judge | Review project links through a clear rubric without score leaks |
| Participant | Receive a spotlight and useful feedback |
| Audience | Follow visible progress and celebrate a fair reveal |
| Operator | Access post-award scores and replay evidence |

## Success criteria

1. A first-time facilitator can turn projects from a workshop, demo session, or
   online build challenge into a complete judging showcase without first organizing
   a formal judging panel.
2. An audience view never shows numerical totals, dimension scores, rank order,
   judge prompts, or unrevealed awards before the award stage.
3. Every project receives a spotlight before the celebration.
4. A saved bundle can be validated and replayed without a model call.
5. A historical rubric-only bundle remains readable.
6. The primary experience uses one visible terminal and never auto-opens a
   second audience or monitor window.
7. The bundled practice showcase completes the full intake-to-replay flow within
   120 seconds under supported local conditions.
8. The final result follows one randomly selected audience-participation cue and
   explicit operator confirmation in an interactive showcase.
9. `PRACTICE SHOWCASE — ILLUSTRATIVE RESULTS` or `OFFICIAL COPILOT PANEL` remains
   visible in the title, opening, run card, act breaks, receipt, and manifest.
10. Installation creates the primary `showcase` command and advanced
    `copilot-builder-showcase` CLI, preserves `hackathon` and `hackathon-judge`,
    and never blocks the showcase when the optional monitor dependency is
    unavailable.
11. Safe HTTP(S) project and demo links work without requiring a GitHub
    repository. Intake never fetches arbitrary non-GitHub URLs.
12. Installed runs connect to the authenticated GitHub Copilot CLI when
    available; `--official` blocks when no compliant live Copilot panel can be
    reached.
13. Native Bash and PowerShell installers provide the same primary and
    compatibility commands on macOS, Linux, and Windows.
14. After awards, each podium project receives a Top-3 growth card with one
    improvement move, one optional Copilot-next suggestion, and one Copilot-use
    summary sourced only from builder-provided evidence.
15. Rapid scorecard judging preserves declared official panel policy and never
    silently downgrades a strict event to a single-model decision.

## Core flow

```text
type showcase and paste project links
        |
        v
resolve and snapshot EventSpec
        |
        v
safe project-link intake with optional GitHub metadata
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
top-three growth cards
        |
        v
recap, validation, and immutable replay archive
```

The intended first-run command is:

```bash
showcase
```

The command collects one safe HTTP(S) project or demo link per line and begins
when the organizer submits an empty line. GitHub `owner/repo` shorthand and
direct arguments are also supported:

```bash
showcase https://demo.example/project-one owner/project-two
```

The complete showcase and commentary appear in one terminal. The current
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
never auto-launched by the showcase. Any operator projection is explicit and
remains unavailable before awards.

## Reliability and integrity

- Project imports are idempotent and preserve GitHub metadata when available.
- Generic project links are never fetched during intake; only their normalized
  URL and organizer-supplied context enter the bundle.
- All initial EventSpec, rubric, input, evaluation, verdict, and award artifacts
  are write-once or append-only as appropriate.
- `freshness_gate.json` records the selected judges and whether the run used an
  Official Copilot Panel or illustrative practice judges.
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

`showcase_launcher.py` is the beginner entry point and routes the default
experience into `builder_showcase.py`, the canonical implementation. The
installer preserves `hackathon` and `hackathon-judge` for compatibility. Existing
bundles with only `config/rubric.json` are read through a legacy adapter; they
do not need to be rewritten to use the current audience and replay surfaces.
