# 🏆 Hackathon Judge

![CI](https://github.com/DUBSOpenHub/hackathon-judge/actions/workflows/ci.yml/badge.svg)
![Version: v3.0.0](https://img.shields.io/badge/version-v3.0.0-5E5E5E.svg)
[![Security Policy](https://img.shields.io/badge/Security-Policy-brightgreen?logo=github)](SECURITY.md)
![Python: 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)

> **A room full of people built something. The ending should feel like a show.**

![Hackathon Judge single-screen Live Show practice demo](docs/images/live-show-demo.png)

Hackathon Judge turns a list of project links into the final act: every build
gets a spotlight, the panel keeps the energy up without leaking scores, and the
awards land with a proper reveal. Send the links. Start the show. Keep the
replay.

Three independent premium models score every project through the same lenses,
then a median consensus locks the public ranking. A separate sealed Shadow Spec
checks deeper quality risks without touching the podium. After the applause,
each project gets useful feedback: what judges liked, a Copilot next move, and
an optional frontier experiment to try.

## ⚡ Install in one command

For people with access to this private repository, paste this into a terminal:

```bash
gh api -H "Accept: application/vnd.github.raw" \
  repos/DUBSOpenHub/hackathon-judge/contents/install.sh | bash
```

It installs the `hackathon-judge` command at `~/.local/bin`. The installer
needs Git, Python 3.11+, and an authenticated [GitHub CLI](https://cli.github.com/)
with repository access. It creates an isolated Python environment and includes
the optional Textual run monitor. The primary Live Show does not depend on that
monitor. If `~/.local/bin` is not already on your `PATH`, the installer prints
the exact command to add it.

### Copilot CLI skill

To run the facilitator experience from Copilot CLI instead, type:

```text
/skills add DUBSOpenHub/hackathon-judge
```

Then say:

```text
run hackathon judging
```

Paste project links with the request and the skill starts the Live Show
immediately. If no links are present, it asks only for the links—there is no mode
picker.

## 🎬 Run your first judging room

Give it project URLs or `owner/repo` entries. This is the happy path for a
screen-shared live event:

```bash
hackathon-judge workshop \
  owner/project-one owner/project-two owner/project-three \
  --run-id spring-demo-day \
  --require-live-terminal \
  --yes
```

Run that command from a real terminal and share the window labeled **LIVE SHOW
— SHARE THIS WINDOW**. The entire run of show, sideline commentary, project
spotlights, audience interaction, awards, and sealing flow happens there.
Hackathon Judge never auto-opens a second window.

At the end, preserve the evidence and let people watch it again:

```bash
hackathon-judge validate spring-demo-day
hackathon-judge replay spring-demo-day
```

## 🎭 Watch the complete practice demo

The bundled practice run uses the same Live Show with three neutral sample
projects:

```bash
hackathon-judge workshop \
  --demo \
  --run-id practice-demo \
  --require-live-terminal \
  --yes
```

It avoids network metadata calls, uses deterministic simulated evaluation, runs
the full intake-to-replay path, and targets completion within 120 seconds.
The target assumes the operator promptly confirms the audience cue; use
`--no-suspense` for unattended smoke automation. Practice awards are
illustrative and must never be presented as official outcomes.

## 🧭 The one experience

```text
bulk links -> intake -> sealed reviews -> project spotlights
           -> audience moment -> bronze/silver/gold -> sealed replay
```

1. **Start:** Bulk GitHub links immediately become project cards.
2. **Review:** Three premium model families evaluate the configured rubric, and
   median consensus locks the public ranking before anyone sees it.
3. **Spotlight:** A generic sideline reporter gives every project a concise,
   data-rich moment with repository signals and specific panel feedback.
4. **Suspense:** Before the final result, the show randomly selects one of ten
   audience cues and waits for the operator to confirm the room is participating.
5. **Reveal:** Bronze, silver, and gold land with a short moment of joy.
6. **Replay:** The show writes its recap, exports and validates the immutable
   bundle, then verifies replay from stored artifacts.

Showtime uses punchy startup demo-day pacing without a named host personality or
publication imitation. Intentional animation is capped at 18 seconds. Review
calls are bounded and parallelized, then reused for verdicts and feedback. Each
run records planned calls and elapsed stages in `eval/plan.json` and
`eval/timing.json`; the bundled demo reports its elapsed time against the
two-minute target.

## 🎛️ Make it yours

Start with the portable event pack:

```bash
cp ~/.local/share/hackathon-judge/config/event.example.json my-event.json
```

It defines the event name, tagline, rubric, review lenses, awards, privacy,
accessibility, and presentation defaults. Then run:

```bash
hackathon-judge workshop \
  --event my-event.json \
  --file submissions.txt \
  --run-id spring-demo-day \
  --require-live-terminal \
  --yes
```

`submissions.txt` contains one GitHub URL or `owner/repo` entry per line. For
team attribution and evidence-based feedback, it can also use:

```text
https://github.com/example/project | Team Aurora | Used Copilot Chat for API design | Built an agent workflow with retrieval | Reduce missed follow-ups | Account executives | https://demo.example/aurora | Daily workflow demo
```

The fields after the URL are optional: team or builder, Copilot-use evidence,
frontier-use evidence, problem statement, intended user, demo or artifact URL,
and builder notes. Hackathon Judge never infers Copilot or frontier use from a
project link or repository metadata; absent evidence is reported as not
provided. Every run snapshots its resolved event configuration, so editing an
event pack later cannot rewrite a past result. Historic rubric-only bundles
remain readable.

The default Live Show uses a bronze → silver → gold podium. Custom EventSpecs
can replace that slate with event-specific awards when the organizer needs a
different outcome. They also declare the tie policy before any project is
scored:

```json
{
  "tie_policy": {
    "mode": "shared-podium",
    "tiebreaker_dimensions": []
  }
}
```

`shared-podium` is the default: tied projects share the medal and the next
numbered place advances under competition placement. Events that need a single
recipient can choose `sealed-tiebreaker` with one or more rubric dimensions, or
`human-resolution` and record a decision with
`--tie-resolution rank:<place>=<submission-id>`. Neither path uses submission
order as a hidden tiebreaker. A human-resolution tie pauses Quick and Live Show
automation at the award stage until that explicit decision is supplied; the
policy sealed with scoring must still match the event snapshot at reveal time.

## 💬 Feedback every builder can use

After a judging run, generate an internal, human-reviewable feedback proposal:

```bash
hackathon-judge feedback spring-demo-day
```

Each project receives what the judges liked, an actionable next step, a concrete
**Copilot next move**, an optional **frontier experiment**, an innovation signal
when the event configured one, and clear Copilot/frontier evidence status. Once
awards are declared, selected projects also receive the specific rationale for
their award. Feedback never calls a model again; it is assembled from the sealed
run artifacts and requires human approval before delivery. Feedback records the
source-labeled project context behind its specific claims; when no project
context can support an idea, that idea is explicitly labeled a **Hypothesis**.

## 🔍 Multi-model consensus and Shadow Spec

The default panel is Claude Opus 4.8, GPT-5.6 Terra, and Gemini 3.1 Pro. Each
model reviews every configured lens; Hackathon Judge takes a median per model
and then a median across the panel, preventing any one evaluator from deciding
the podium. Exact ties follow the EventSpec’s sealed tie policy—shared podium
by default, a predeclared tiebreaker, or a logged human decision—never input
order.

Before projects are scored, the engine seals a task-bound **Shadow Spec** with
hidden checks for brief alignment, evidence calibration, unsupported claims,
scope discipline, task-specific edge cases, and a leakage decoy. Its assessment
is diagnostic-only: it never changes public scores, rankings, or awards. The
criteria and quality report stay hidden until the event is awarded.

## 🔒 Fair by default

| Guardrail | What it means |
| --- | --- |
| Sealed scores | Scores and rankings stay hidden until awards are declared. |
| Single audience surface | The one shared Live Show terminal never shows unrevealed scores, ranks, prompts, awards, or model-authored intake details. |
| Equal spotlight | Every accepted project appears before the ceremony. |
| Declared tie policy | Ties share the podium by default; any tiebreaker or human decision is recorded in the award artifact. |
| Evidence, not inference | Copilot and frontier claims require builder-provided evidence. |
| Grounded feedback | Project-specific feedback cites supplied intake context; unsupported ideas are labeled hypotheses. |
| Multi-model consensus | Three independent premium model families contribute to each public score; median consensus determines the result. |
| Diagnostic Shadow Spec | A sealed hidden-quality assessment can flag calibration, scope, or leakage concerns without affecting awards. |
| Provenance | Each bundle records whether reviews came from a configured live model gateway or deterministic simulation. |
| Read-only replay | Replays read stored artifacts only; they never call a model. |
| Tamper evidence | Exported bundles include `HASHES` and `SEAL` integrity records. |

Treat run bundles as internal artifacts: they can contain project metadata and
judge feedback. See [SECURITY.md](SECURITY.md) for reporting guidance and
platform safeguards.

## 🧰 Command reference

| Command | Use it for |
| --- | --- |
| `hackathon-judge workshop ... --require-live-terminal --yes` | Running the complete single-screen Live Show. |
| `hackathon-judge workshop --demo ...` | Running the same Live Show with deterministic bundled practice projects. |
| `hackathon-judge import-urls <run-id> ...` | Adding a batch of GitHub projects to an existing run. |
| `hackathon-judge judge <run-id>` | Running the sealed evaluation stage. |
| `hackathon-judge present <run-id>` | Presenting stored artifacts from a run bundle. |
| `hackathon-judge tui <run-id> --projector` | Opening the optional Textual run monitor; it is never auto-launched. |
| `hackathon-judge award <run-id> --winner <id>` | Declaring the winner after the review and spotlight stages; add `--tie-resolution rank:<place>=<id>` for a human-resolution event. |
| `hackathon-judge feedback <run-id>` | Writing private, human-reviewable per-project feedback from sealed artifacts. |
| `hackathon-judge export <run-id>` | Packaging an immutable result bundle. |
| `hackathon-judge validate <run-id>` | Verifying bundle hashes and seals. |
| `hackathon-judge replay <run-id>` | Replaying a prior event without model calls. |
| `hackathon-judge doctor` | Checking configuration, model-gate, and bundle health. |

## 🏗️ How it is built

```text
EventSpec -> intake -> multi-model consensus -> sealed artifacts -> audience view -> awards -> export/replay
```

- `hackathon_judge.py` is the canonical CLI and bundle writer.
- `hackathon_judge_dashboard.py` is the optional Textual run monitor.
- `bundle_reader.py` creates separate audience-safe and operator views.
- `event_spec.py` resolves neutral event configuration and preserves legacy
  rubric-only bundles.

The core engine uses the Python standard library. The installer also adds
[Textual](https://textual.textualize.io/) for the optional monitor. Developers
running directly from a checkout may install `textual>=8,<9`; its absence does
not block the primary Live Show.

## 🧪 Develop

```bash
python3 -m pytest -q
python3 -m py_compile hackathon_judge.py hackathon_judge_dashboard.py event_spec.py bundle_reader.py
```

## 🤝 Contributing

Keep the default experience neutral and audience-safe. Do not commit run
bundles, credentials, or confidential project metadata. The working
invariants and canonical surfaces are in [AGENTS.md](AGENTS.md).

## 💜 Credits

Created with care by [@DUBSOpenHub](https://github.com/DUBSOpenHub) with the
[GitHub Copilot CLI](https://docs.github.com/copilot/concepts/agents/about-copilot-cli).
