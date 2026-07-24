<div align="center">

# 🏆 Copilot Builder Showcase

**Turn the awkward final ten minutes of any workshop into a live, Copilot-judged finale.**

Paste the links, activate the panel, reveal the winners — in under two minutes.

[![GitHub](https://img.shields.io/badge/GitHub-Copilot_CLI-blue?logo=github)](https://github.com/features/copilot)
[![CI](https://github.com/DUBSOpenHub/copilot-builder-showcase/actions/workflows/ci.yml/badge.svg)](https://github.com/DUBSOpenHub/copilot-builder-showcase/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Security Policy](https://img.shields.io/badge/Security-Policy-brightgreen?logo=github)](SECURITY.md)

**[Live demo](https://dubsopenhub.github.io/copilot-builder-showcase/) · [What it does](#what-it-does) · [Install](#install) · [How it works](#how-it-works) · [From the CLI](#from-the-cli) · [FAQ](#faq) · [License](#license-and-credits)**

<img src="docs/images/builder-showcase-demo.svg" alt="Copilot Builder Showcase big-screen award reveal: the Copilot Builder Award, Builder Silver, and Builder Bronze podium with per-lens Copilot panel scores" width="100%">

*Every accepted project gets a spotlight. One sealed Copilot panel scores them all on the same rubric. Then the room watches the podium land — Builder Bronze, Builder Silver, and the first-place Copilot Builder Award.*

</div>

---

> ### ⚡ One command. That's it.
>
> **Never used Copilot Builder Showcase before?**
>
> 1. Open a terminal.
> 2. On macOS or Linux, paste:
>    ```bash
>    bash -o pipefail -c 'gh api repos/DUBSOpenHub/copilot-builder-showcase/contents/install.sh \
>      -H "Accept: application/vnd.github.raw+json" | bash'
>    ```
>    On Windows, use the PowerShell command in [Install](#install).
> 3. Type:
>    ```bash
>    showcase
>    ```
>    If that command is not on your PATH yet, run `~/.local/bin/showcase` on macOS/Linux or `$HOME\.local\bin\showcase.exe` on Windows.
> 4. Paste project or demo links, one per line. Press Return on an empty line.
>
> *Requires Git and Python 3.11+ for practice installs. The one-line install prefers an authenticated GitHub CLI when available. Official judging uses an authenticated GitHub Copilot CLI.*

---

## What it does

Builder workshops, demo days, conference sessions, and online challenges are great at *starting* projects and terrible at *ending* them. The links scatter across chat threads and browser tabs. A few people get demo time. Most projects get no real review. The room has nothing to follow — and then the session just... stops.

Copilot Builder Showcase is a one-command finale for that moment. The host pastes the links (or the room scans a QR to submit). A sealed Copilot panel reviews every project through the same rubric. Each entry gets a spotlight, the audience joins one quick reveal cue, and a real podium lands — saved as a replayable bundle.

It gives any session the energy and resolution of a judged finale, without recruiting a single human judge.

| Before | With Copilot Builder Showcase |
|---|---|
| Links sit in a chat or spreadsheet | The host pastes the complete list once — or the room submits by QR |
| Only a few people get demo time | Every accepted project gets a spotlight |
| Review quality depends on who speaks up | Every project uses the same declared rubric |
| The session ends after the final demo | The room joins a short recognition reveal |
| Feedback and decisions disappear | Awards, feedback, validation, and replay stay together |

---

## What you do in a showcase

- **Drop the projects.** Paste HTTP(S) links and GitHub `owner/repo` entries — or let the room scan a QR and submit from their phones.
- **Run one sealed panel.** A compact Copilot scorecard applies Innovation, Build Quality, and Impact lenses to every project. Scores stay sealed.
- **Give everyone a moment.** Each project gets a spotlight with three brief judge reactions — no scores, no rank, no one left out.
- **Bring the room in.** One operator-confirmed audience cue — a drumroll, a countdown, an applause check — right before the reveal.
- **Land the podium.** Third, second, then the first-place Copilot Builder Award, with per-lens panel scores revealed only now.
- **Keep the work.** Recap, private per-project feedback, validation, export, and replay are saved together. Top-three growth cards follow the awards.

The whole thing runs in one visible Terminal, with a big-screen web view for the room. It never auto-opens a second window.

---

## A look inside

<table>
  <tr>
    <td width="50%" valign="top">
      <img src="docs/images/look-scan-submit.svg" alt="A room QR on the big screen and a phone showing the add-your-project form" width="100%"><br><br>
      <strong>Scan to submit.</strong> The big screen shows a room QR. Anyone adds a project from their phone — no app, no login, just a link and an optional team name.
    </td>
    <td width="50%" valign="top">
      <img src="docs/images/look-sealed-panel.svg" alt="Three Copilot judge scorecards with scores sealed, above a spotlighted project" width="100%"><br><br>
      <strong>One sealed panel.</strong> Every project is reviewed on the same rubric and combined by median consensus. Scores stay sealed until the reveal.
    </td>
  </tr>
</table>

*The finale up top is the payoff — this is how the room gets there.*

---

## A real podium, useful feedback

Every project receives a brief on-screen review. Only the top three receive awards:

| Award | Selection |
|---|---|
| 🥉 **Third — Builder Bronze** | Third-highest complete result |
| 🥈 **Second — Builder Silver** | Second-highest complete result |
| 🏆 **First — Copilot Builder Award** | Strongest complete result |

Each award explains why the project placed, what the panel uniquely liked, and one specific level-up move. After the reveal, top-three growth cards add one improvement move, one optional Copilot-next move, and one Copilot-use summary sourced only from builder-provided evidence.

Exact ties follow the event's declared policy — shared placement, a predeclared rubric tiebreaker, or a logged human decision — never input order. Formal events can swap this slate for a traditional podium or any custom recognitions in the EventSpec.

---

## Fair by default

| Guardrail | What it means |
|---|---|
| Persistent result status | Practice or Official status stays visible throughout the showcase and in the manifest. |
| Sealed scores | Scores, rankings, prompts, and unrevealed awards stay hidden until the reveal. |
| Equal spotlight | Every accepted project appears before the recognition ceremony. |
| Same declared rubric | Every entry uses the same snapshotted review policy. |
| Multi-model consensus | Official panels combine configured judges using median consensus. |
| Strict official policy | Rapid scorecards keep the configured panel policy and never silently downgrade to one model. |
| Evidence, not inference | Copilot and frontier claims require builder-provided evidence. |
| Hidden diagnostic review | A sealed Shadow Spec review can warn the organizer but cannot alter winners. |
| Read-only replay | Replays use stored artifacts and never call a judge. |
| Tamper evidence | Exported bundles include `HASHES` and `SEAL` integrity records. |

Run bundles can contain project context and judge feedback — treat them as internal artifacts unless a human approves publication. See [SECURITY.md](SECURITY.md).

<details>
<summary><strong>The hidden quality review, explained</strong></summary>

<br>

The visible rubric is not the only check. Before judging, Copilot Builder Showcase creates a second *sealed* diagnostic review. After the panel finishes, it looks for unsupported claims, shallow evidence, rubric gaps, calibration problems, and score leakage.

This hidden review stays sealed until the awards stage, never changes public scores or awards, and gives the organizer private warnings when a result deserves another look. It follows the sealed-envelope principles behind [Shadow Score Spec](https://github.com/DUBSOpenHub/shadow-score-spec) — without turning hidden criteria into a secret way to pick winners.

</details>

---

## Works today · On the roadmap · Opinions we hold

| ✅ Works today | 🚧 On the roadmap | 💭 Opinions we hold |
|---|---|---|
| One-command install on macOS, Linux, and Windows | Turnkey hosted backend for QR live-submit | "Copilot-judged," never hand-wavy "AI-judged" |
| Offline practice showcase with a bundled demo | More built-in EventSpec templates | No scores or ranks before the reveal — ever |
| Official Copilot panel via authenticated Copilot CLI | Deeper accessibility narration | One operator-confirmed cue before the podium |
| Paste links **or** scan a QR to submit *(optional backend)* | Richer big-screen theming | One visible Terminal; never a surprise second window |
| Sealed median-consensus scoring | | Evidence over inference for Copilot/frontier claims |
| Bronze / Silver / Copilot Builder Award podium | | Every accepted project gets a spotlight |
| Private feedback + top-three growth cards | | |
| Replay & validate from tamper-evident bundles | | |
| Reduced-motion reveal pacing | | |

---

## Install

### Instant install

**macOS or Linux**

```bash
bash -o pipefail -c 'gh api repos/DUBSOpenHub/copilot-builder-showcase/contents/install.sh \
  -H "Accept: application/vnd.github.raw+json" | bash'
```

**Windows PowerShell**

```powershell
$installer = Join-Path $env:TEMP "install-copilot-builder-showcase.ps1"
gh api repos/DUBSOpenHub/copilot-builder-showcase/contents/install.ps1 `
  -H "Accept: application/vnd.github.raw+json" > $installer
if ($LASTEXITCODE -ne 0) { throw "Installer download failed." }
powershell -ExecutionPolicy Bypass -File $installer
```

Then run:

```bash
showcase
```

The installer:

- checks for Git and Python 3.11+,
- installs into `~/.local/share/copilot-builder-showcase`,
- creates `showcase` and `copilot-builder-showcase` launchers in `~/.local/bin`,
- preserves `hackathon` and `hackathon-judge` as compatibility aliases,
- never edits your shell profile,
- and treats the optional Textual monitor as non-blocking.

> 💡 **Security note:** Inspect [install.sh](install.sh) or
> [install.ps1](install.ps1) before execution if that is your preferred
> installation policy.

### Clone and install

**macOS or Linux**

```bash
gh repo clone DUBSOpenHub/copilot-builder-showcase
cd copilot-builder-showcase
bash install.sh
```

**Windows PowerShell**

```powershell
gh repo clone DUBSOpenHub/copilot-builder-showcase
Set-Location copilot-builder-showcase
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Windows 10 or 11 with Windows Terminal or a modern PowerShell host is recommended for the Unicode ceremony and ANSI color output. For an Official Copilot Panel on Windows, install the native CLI with `winget install GitHub.Copilot`. Copilot Builder Showcase requires `copilot.exe` for official Windows judging and rejects `.cmd` or `.bat` shims so project text never passes through `cmd.exe`. Practice showcases do not require Copilot CLI.

---

## Getting started — pick your path

Not sure where to begin? Find the row that sounds like you.

### 🎤 "I'm running a workshop."

1. Install once (see [Install](#install) above).
2. When the room finishes building, start the finale:
   ```bash
   showcase
   ```
3. Paste the project links — one link or the whole batch at once — **or** put the [big-screen live demo](https://dubsopenhub.github.io/copilot-builder-showcase/) on the projector and let people scan the QR to submit.
4. Trigger the audience cue, then reveal the podium.

Every event after that is just `showcase` again.

### 🧪 "I just want to see it work."

```bash
showcase --demo
```

The bundled three-project demo runs offline in under two minutes, follows the same intake-to-reveal flow, and is always labeled `PRACTICE SHOWCASE — ILLUSTRATIVE RESULTS`. No Copilot calls, no network metadata.

### 🛠️ "I want official judging or to customize."

Connect an authenticated Copilot panel:

```bash
copilot login
showcase doctor
showcase --official https://example.com/project-one https://example.com/project-two
```

Then bring your own rubric, recognitions, and panel policy with a custom EventSpec — see [Going further](#going-further).

---

## Project links are enough

The beginner input can mix any safe HTTP(S) project or demo links:

```text
https://github.com/example/project-one
example/project-two
https://demo.example.com/aurora
https://example.net/showcase/project-three
```

GitHub links may use public repository metadata for a richer spotlight. Generic links are not fetched by the intake process; Copilot Builder Showcase derives a safe project label from the URL and uses only the context the organizer supplies.

For advanced events, add optional context after each link:

```text
https://demo.example/aurora | Team Aurora | Used Copilot for API design | Built a retrieval agent | Reduce missed follow-ups | Account executives | https://demo.example/aurora | Daily workflow demo
```

The optional fields are team or builder, Copilot evidence, frontier evidence, problem statement, intended user, demo or artifact URL, and builder notes. Copilot Builder Showcase never infers Copilot or frontier-model use from a link, code, metadata, or judge impression.

---

## How it works

```text
EventSpec -> project intake -> consensus review -> sealed artifacts
          -> audience-safe showcase -> recognitions -> export/replay
```

One compact Copilot scorecard applies the Innovation, Build Quality, and Impact lenses to every project. Official panels combine one to three configured Copilot models by median consensus and never silently downgrade to a single model. Everything the run produces — scores, feedback, validation, replay — is sealed into a tamper-evident bundle.

Every run keeps its status visible:

| Status | Meaning |
|---|---|
| **PRACTICE SHOWCASE — ILLUSTRATIVE RESULTS** | Deterministic local judges are active. For rehearsal and demonstration, not official awards. |
| **OFFICIAL COPILOT PANEL** | A connected Copilot panel reviewed the projects. |

<details>
<summary><strong>The files behind it</strong></summary>

<br>

- `showcase_launcher.py` — the beginner `showcase` entry point
- `builder_showcase.py` — the canonical engine and advanced CLI
- `builder_showcase_dashboard.py` — the optional Textual run monitor
- `bundle_reader.py` — audience-safe and operator views
- `event_spec.py` — portable event configuration and legacy bundle support

The primary live showcase uses only the Python standard library. Textual is optional, and an install failure never blocks the showcase.

</details>

---

## Going further

<details>
<summary><strong>Command reference</strong></summary>

<br>

| Command | Use it for |
|---|---|
| `showcase` | Paste links and start the complete live showcase. |
| `showcase <links...>` | Start immediately with supplied projects. |
| `showcase --demo` | Run the bundled two-minute practice showcase. |
| `showcase --reduced-motion` | Prefer low-motion reveal pacing (or set `CBS_REDUCED_MOTION=1`; legacy `HJ_REDUCED_MOTION` still honored). |
| `showcase --official <links...>` | Require a connected official Copilot panel. |
| `showcase replay <run-id>` | Replay a prior showcase without judge calls. |
| `showcase validate <run-id>` | Verify bundle hashes and seals. |
| `showcase feedback <run-id>` | Write private per-project feedback. |
| `showcase doctor` | Check setup, panel connection, and bundle health. |
| `showcase present <run-id> --operator` | Inspect stored results after awards. |
| `showcase tui <run-id> --projector` | Open the optional diagnostic monitor. |

The installer also preserves `hackathon` and `hackathon-judge` as compatibility aliases for existing scripts.

</details>

<details>
<summary><strong>Connect an official Copilot panel</strong></summary>

<br>

Official judging runs through your authenticated [GitHub Copilot CLI](https://docs.github.com/copilot/how-tos/copilot-cli). Sign in once, then check the panel:

```bash
copilot login
showcase doctor
showcase --official https://example.com/project-one https://example.com/project-two
```

No separate model API key or provider SDK is required. Copilot Builder Showcase invokes Copilot in non-interactive, tool-free mode and disables repository instructions, built-in MCP servers, remote control, and file or shell tools for every judge call.

The default EventSpec declares a three-family Copilot panel. You can configure one to three Copilot model IDs, the minimum panel size, provider diversity, concurrency, and reasoning requirements in your event file. If `--official` is used without an authenticated Copilot CLI, the command blocks — it never silently converts an official event into a practice result, and never falls back to a hidden single-model panel.

</details>

<details>
<summary><strong>Customize the event (EventSpec)</strong></summary>

<br>

Copy the starter EventSpec:

```bash
cp ~/.local/share/copilot-builder-showcase/config/event.example.json my-event.json
```

Run the same showcase with your configuration:

```bash
showcase \
  --event my-event.json \
  --file submissions.txt \
  --run-id spring-demo-day \
  --require-live-terminal \
  --yes
```

The EventSpec defines event name and tagline, scoring dimensions and review lenses, recognition categories and tie behavior, privacy and accessibility defaults, live-panel models and policy, presentation behavior, and the diagnostic Shadow Spec. Every run snapshots its resolved configuration so later edits cannot rewrite a past result.

</details>

<details>
<summary><strong>After the showcase — replay, validate, feedback</strong></summary>

<br>

Runs are stored under:

```text
~/.copilot_builder_showcase/runs/<run-id>/
```

Useful commands:

```bash
showcase replay <run-id>
showcase validate <run-id>
showcase feedback <run-id>
showcase doctor
showcase present <run-id> --operator
```

Private feedback includes what judges liked, an actionable next step, an optional Copilot next move, and a bounded frontier experiment. Unsupported ideas are labeled as hypotheses.

</details>

---

## From the CLI

Use it straight from Copilot CLI. Add the skill:

```text
/skills add DUBSOpenHub/copilot-builder-showcase
```

Then type:

```text
showcase
```

The skill checks whether the command is installed, asks permission before installing anything, runs `showcase doctor`, and opens exactly one real terminal window for the audience experience.

---

## FAQ

**Do I need an API key?**
No separate API key is required. The practice showcase is local and illustrative; an Official Copilot Panel uses your authenticated GitHub Copilot CLI subscription.

**What links can I paste?**
Any safe HTTP(S) project or demo URL, plus GitHub `owner/repo` shorthand.

**Does it open every website I paste?**
No. Generic non-GitHub links are not fetched during intake. GitHub repository links may use public metadata when available.

**How are winners chosen?**
The panel scores the declared rubric, official model responses are combined by median consensus, and recognitions follow their declared dimensions and tie policy.

**What does the hidden Shadow review do?**
It independently checks review quality and possible failure modes. It can warn the organizer, but it cannot change scores, rankings, or awards.

**Can I replay a showcase without spending more model calls?**
Yes. Replay reads the sealed bundle and never contacts a judge.

**Is the output public?**
No. Run bundles and feedback are internal by default. A human must approve external publishing.

---

## What it is not

Copilot Builder Showcase is **not**:

- **A regulated or legally consequential decision-maker** — use qualified human judges and the required review process.
- **A place for secrets or restricted data** — do not submit context that cannot be shared with the configured model provider.
- **Expert certification** — a Copilot panel is not a substitute for domain accreditation, safety review, or compliance approval.
- **A one-project feedback tool** — for a single review, a direct read is simpler than producing a live group finale.

**What it is:** a fast, fair, watchable ending for shared building — recognition, useful feedback, and a replayable record. Human approval remains required before anything goes public.

---

## Contributing

Keep the default experience neutral, beginner-first, and audience-safe. Do not commit run bundles, credentials, or confidential project metadata. See [AGENTS.md](AGENTS.md) for the working invariants.

```bash
python3 -m pytest -q
python3 -m py_compile showcase_launcher.py builder_showcase.py builder_showcase_dashboard.py event_spec.py bundle_reader.py
bash -n install.sh
```

---

<div align="center">

## License and credits

**[MIT](LICENSE)** — use it, fork it, build on it.

🐙 Created with 💜 by **[@DUBSOpenHub](https://github.com/DUBSOpenHub)** with the **[GitHub Copilot CLI](https://docs.github.com/copilot/concepts/agents/about-copilot-cli)**.

</div>
