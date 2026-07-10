# Hackathon Judge

> **Paste the project links. Get a live judging show instead of a spreadsheet.**

Most hackathons end at the awkward part: someone opens a tab full of project
links, judges compare notes in private, and the audience waits for a winner
they cannot understand. The projects were the show. The judging becomes
administration.

Hackathon Judge turns a list of GitHub links into a fair, screen-shareable
event: every project gets a spotlight, scores stay sealed until the reveal, and
the final result becomes a replayable bundle instead of a pile of screenshots.

The wedge is intentionally small. An organizer pastes project links. Everyone
else gets a better ending.

## Run a show

```bash
cp config/event.example.json my-event.json

python3 hackathon_judge.py workshop \
  --event my-event.json \
  --file submissions.txt \
  --run-id spring-hackathon \
  --projector \
  --yes
```

`submissions.txt` accepts one GitHub URL or `owner/repo` entry per line.

For a separate big-screen board:

```bash
python3 hackathon_judge.py tui spring-hackathon --projector
```

## What the room sees

```text
project links -> project intake -> sealed reviews -> spotlights -> award reveal
```

Before awards, the audience sees project progress, metadata, and encouraging
feedback. They do not see numeric scores, rankings, judge prompts, or
unrevealed awards. After the reveal, an operator can inspect scores with:

```bash
python3 hackathon_judge.py present spring-hackathon --operator
```

## Why this exists

The number of people building at hackathons keeps growing while the final
judging experience has barely changed. Builders now arrive with working
repositories, demos, agents, and real users. The last mile is still a
spreadsheet and a whispered ranking.

Hackathon Judge makes the last mile visible, legible, and worth watching.

## Make it yours

`config/event.example.json` is a portable EventSpec. Give each event its own:

- Name, tagline, rubric, and neutral review lenses
- Awards and award language
- Score-visibility, projector, privacy, and accessibility defaults
- Premium model policy

Every run snapshots its resolved EventSpec at `config/event.json`. Changing an
event pack later cannot rewrite a past outcome. Historic rubric-only bundles
remain readable.

## The trust model

- Scoring is sealed before the reveal.
- Audience views mask scores and rankings until awards are declared.
- `freshness_gate.json` records the chosen model and whether a run was live or
  deterministic simulation.
- Replays use stored artifacts only; they never make new model calls.
- Exported bundles contain `HASHES` and `SEAL` for integrity verification.
- Project metadata and winner materials are internal until a human approves
  external publication.

## Commands

```bash
# Build an event in stages
python3 hackathon_judge.py init spring-hackathon --event my-event.json
python3 hackathon_judge.py import-urls spring-hackathon --file submissions.txt
python3 hackathon_judge.py judge spring-hackathon --showtime
python3 hackathon_judge.py present spring-hackathon --projector
python3 hackathon_judge.py award spring-hackathon --winner <submission_id> --showtime

# Preserve and replay the result
python3 hackathon_judge.py recap spring-hackathon
python3 hackathon_judge.py export spring-hackathon
python3 hackathon_judge.py validate spring-hackathon
python3 hackathon_judge.py replay spring-hackathon
```

## Develop

```bash
python3 -m pytest -q
python3 -m py_compile hackathon_judge.py hackathon_judge_dashboard.py event_spec.py bundle_reader.py
```

Python 3.11+ is required. The core engine uses the standard library; Textual
is optional for the live board.

## Security

Treat run bundles as internal artifacts. They can contain project metadata and
judge feedback. See [SECURITY.md](SECURITY.md) for reporting guidance.
