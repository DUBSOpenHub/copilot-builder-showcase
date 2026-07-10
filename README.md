# 🏆 Hackathon Judge

![CI](https://github.com/DUBSOpenHub/hackathon-judge/actions/workflows/ci.yml/badge.svg)
![Version: v2.1.0](https://img.shields.io/badge/version-v2.1.0-5E5E5E.svg)
[![Security Policy](https://img.shields.io/badge/Security-Policy-brightgreen?logo=github)](SECURITY.md)
![Python: 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)

> **Paste project links. Put the judging room on screen. Let every builder get a moment.**

A room full of people built something. The ending should feel like a show.

Hackathon Judge turns a list of project links into the final act: every build
gets a spotlight, the panel keeps the energy up without leaking scores, and
the awards land with a proper reveal. Send the links. Start the show. Keep the
replay.

## ⚡ Install in one command

For people with access to this private repository, paste this into a terminal:

```bash
gh api -H "Accept: application/vnd.github.raw" \
  repos/DUBSOpenHub/hackathon-judge/contents/install.sh | bash
```

It installs the `hackathon-judge` command at `~/.local/bin`. The installer
needs Git, Python 3.11+, and an authenticated [GitHub CLI](https://cli.github.com/)
with repository access. If `~/.local/bin` is not already on your `PATH`, the
installer prints the exact command to add it.

### Copilot CLI skill

To run the facilitator experience from Copilot CLI instead, type:

```text
/skills add DUBSOpenHub/hackathon-judge
```

Then say:

```text
run hackathon judging
```

## 🎬 Run your first judging room

Give it project URLs or `owner/repo` entries. This is the happy path for a
screen-shared live event:

```bash
hackathon-judge workshop \
  owner/project-one owner/project-two owner/project-three \
  --run-id spring-demo-day \
  --projector \
  --yes
```

Share the terminal or launch a separate projector board:

```bash
hackathon-judge tui spring-demo-day --projector
```

At the end, preserve the evidence and let people watch it again:

```bash
hackathon-judge validate spring-demo-day
hackathon-judge replay spring-demo-day
```

## 🧭 What happens in the room

```text
project links -> intake -> sealed reviews -> every-project spotlight -> awards -> replay
```

1. **Intake:** GitHub URLs become consistent project cards.
2. **Review:** The configured event rubric evaluates projects before anyone sees
   a ranking.
3. **Spotlight:** The room sees project progress, metadata, and encouraging
   feedback -- not numeric scores.
4. **Reveal:** The operator declares awards after every project has had its
   moment.
5. **Replay:** A sealed bundle preserves the outcome without generating new
   reviews.

Showtime keeps the ceremony brisk: a few panel beats, one quick reaction per
spotlight, then the reveal. Intentional animation pauses are capped below a
minute; review time itself scales with the number of submitted projects.

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
  --projector \
  --yes
```

`submissions.txt` contains one GitHub URL or `owner/repo` entry per line. Every
run snapshots its resolved event configuration, so editing an event pack later
cannot rewrite a past result. Historic rubric-only bundles remain readable.

## 🔒 Fair by default

| Guardrail | What it means |
| --- | --- |
| Sealed scores | Scores and rankings stay hidden until awards are declared. |
| Audience-safe projector | The audience never sees unrevealed scores, ranks, prompts, or awards. |
| Equal spotlight | Every accepted project appears before the ceremony. |
| Provenance | Each bundle records whether reviews came from a configured live model gateway or deterministic simulation. |
| Read-only replay | Replays read stored artifacts only; they never call a model. |
| Tamper evidence | Exported bundles include `HASHES` and `SEAL` integrity records. |

Treat run bundles as internal artifacts: they can contain project metadata and
judge feedback. See [SECURITY.md](SECURITY.md) for reporting guidance and
platform safeguards.

## 🧰 Command reference

| Command | Use it for |
| --- | --- |
| `hackathon-judge workshop ...` | Running the complete live flow from links to awards. |
| `hackathon-judge import-urls <run-id> ...` | Adding a batch of GitHub projects to an existing run. |
| `hackathon-judge judge <run-id>` | Running the sealed evaluation stage. |
| `hackathon-judge present <run-id> --projector` | Presenting stored artifacts safely to an audience. |
| `hackathon-judge tui <run-id> --projector` | Opening the big-screen Textual board, with a CLI fallback. |
| `hackathon-judge award <run-id> --winner <id>` | Declaring the winner after the review and spotlight stages. |
| `hackathon-judge export <run-id>` | Packaging an immutable result bundle. |
| `hackathon-judge validate <run-id>` | Verifying bundle hashes and seals. |
| `hackathon-judge replay <run-id>` | Replaying a prior event without model calls. |
| `hackathon-judge doctor` | Checking configuration, model-gate, and bundle health. |

## 🏗️ How it is built

```text
EventSpec -> intake -> evaluation -> sealed artifacts -> audience view -> awards -> export/replay
```

- `hackathon_judge.py` is the canonical CLI and bundle writer.
- `hackathon_judge_dashboard.py` is the optional Textual projector board.
- `bundle_reader.py` creates separate audience-safe and operator views.
- `event_spec.py` resolves neutral event configuration and preserves legacy
  rubric-only bundles.

The core engine uses the Python standard library. Install
[Textual](https://textual.textualize.io/) for the enhanced dashboard; the
projector command falls back to the CLI presenter when it is unavailable.

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
