---
name: hackathon-judge
description: >
  Give any builder workshop, product demo, conference build session, online
  challenge, or hackathon a shared ending. Paste project links and create one
  single-screen AI-panel show where every build gets a spotlight, the audience
  joins the reveal, and the result is preserved. Say "hackathon" to start.
tools:
  - bash
  - ask_user
---

# Hackathon Judge

The primary experience is one Live Show. Use the installed `hackathon` command;
never ask a beginner to run Python or know the internal `workshop` subcommand.
Never invent an outcome in prose.

## Triggers

- `hackathon`
- `hackathon judge`
- `run hackathon judging`
- `judge these projects`
- `judge these repos`
- `judge these demos`
- `wrap up this builder workshop`
- `turn these projects into a show`
- `run the panel`
- `run a hackathon judge demo`

## First-run setup

Before collecting projects, check for the command:

```bash
command -v hackathon
```

If it is missing:

1. Explain in one sentence that installation downloads this repository into
   `~/.local/share/hackathon-judge` and creates commands in `~/.local/bin`.
2. Use `ask_user` to request installation permission.
3. Do not install unless the user explicitly approves.
4. When approved, run:

   ```bash
   gh api -H "Accept: application/vnd.github.raw" \
     repos/DUBSOpenHub/hackathon-judge/contents/install.sh | bash
   ```

5. Use `~/.local/bin/hackathon` for the current run even if the shell has not
   reloaded its PATH.
6. Run `~/.local/bin/hackathon doctor`. If it fails, stop and report the
   specific setup issue before accepting projects.

If installation is declined, provide the install command and stop. Never change
shell profiles automatically.

## One experience

- Do not offer Live, Quick, or Slack judging as mode choices.
- If project links or an uploaded submissions file are already present, start
  the Live Show immediately.
- If no project links are present, ask only for the links.
- Accept plain GitHub URLs or `owner/repo` entries.
- If the organizer says `run again` or `start over`, reuse the previous project
  entries with a fresh run ID.
- If the organizer asks for a demo without links, use `hackathon --demo`.

Plain links are enough. Hackathon Judge automatically uses public repository
context and labels an unnamed entry as `<repository owner> team`. Never infer
Copilot or frontier use from a link, code, metadata, or a judge impression.
Missing evidence stays `not provided`.

## Result status

Keep the show's result status explicit:

- `PRACTICE SHOW — ILLUSTRATIVE RESULTS` means local practice judges are active.
- `OFFICIAL LIVE PANEL` means a host connected live judges.

Installed local runs are Practice Shows unless an official host integration is
connected. If the organizer requires an official event, add `--official`; the
command must block rather than silently produce practice results.

## Start the Live Show

Write supplied links to a temporary file. Use the absolute command path when
needed:

```bash
~/.local/bin/hackathon \
  --file <temporary-submissions-file> \
  --run-id <safe-event-run-id> \
  --require-live-terminal \
  --yes
```

On macOS, open exactly one real Terminal using that command:

```bash
osascript \
  -e 'tell application "Terminal" to do script "<shell-quoted-absolute-hackathon-command>"' \
  -e 'tell application "Terminal" to activate'
```

Shell-quote every generated path and argument. Never place untrusted project
text directly into the AppleScript command; pass it through the temporary file.

The new Terminal contains the complete audience experience. Share that one
window. Never auto-open the optional Textual monitor or a second Terminal.
Captured tool output is not the audience show; if a real Terminal cannot be
opened, stop before judging and provide the exact manual `hackathon` command.

## Run the two-minute practice demo

Use the same Live Show:

```bash
~/.local/bin/hackathon \
  --demo \
  --run-id <safe-demo-run-id> \
  --require-live-terminal \
  --yes
```

The demo is deterministic, avoids network metadata calls, exercises the full
intake-to-replay flow, and targets completion within 120 seconds. It is always
illustrative and never an official competition result.

## Show direction

The Live Show should feel like a punchy startup demo day:

1. Project links enter immediately.
2. A generic sideline reporter describes the action with short, energetic lines.
3. Every project receives a data-rich spotlight and a specific panel reaction.
4. Scores, ranks, prompts, and awards stay sealed.
5. Before the final result, select one of the ten audience-participation cues,
   ask the operator to confirm the room is participating, then reveal.
6. Finish with a concise moment of joy, recap, export, validation, and replay.

Use suspense without a named host personality or publication imitation. Keep
the ceremony concise enough for a two-minute demo.

## Audience safety

- Never expose numeric scores, ranks, judge prompts, unrevealed awards, or the
  sealed Shadow Spec before awards.
- Every accepted project must appear before the ceremony.
- Keep Practice or Official status visible throughout.
- The optional `tui` command is diagnostic-only and must never auto-launch.
- Use `present <run-id> --operator` only after awards when scores are needed
  privately.

## Awards, ties, and feedback

The default reveal is project-first bronze → silver → gold. Exact ties follow
the EventSpec policy: shared podium, a predeclared sealed tiebreaker, or a logged
human decision. Never use entry order as a tiebreaker.

Private feedback may include award rationale, what judges liked, one actionable
next step, a Copilot next move, a bounded frontier experiment, and explicit
evidence status. Project-specific claims must use supplied context; unsupported
suggestions must be labeled hypotheses.

## After the run

Report the result status, run ID, bundle path, private feedback path, awards and
winners, replay command, and validation status. Keep run bundles internal unless
a human approves external publishing.
