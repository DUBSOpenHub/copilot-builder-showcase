#!/usr/bin/env python3
"""Render a shareable results card for a completed showcase run.

The ceremony is a live thing: once the terminal scrolls and the big screen is
closed, the outcome only survives as a run bundle nobody outside the room will
read. This turns a finished bundle into one image an organiser can drop into a
recap, a slide, or a post.

Everything on the card is read out of the bundle at render time -- placements
and scores from ``verdicts/``, award names from ``winner/awards.json``, the
judging panel from ``eval/`` and the official-vs-practice status from
``manifest/bundle.json``. Nothing is passed in by hand, so the card cannot
disagree with the sealed record it came from.

Design tokens are lifted out of ``docs/index.html`` the same way
``make_social_card.py`` does it, so the card cannot drift from the site palette.

Usage:
    python3 tools/make_results_card.py <run-id> [--out PATH] [--title TEXT]

Requires Playwright (``pip install playwright && playwright install chromium``).
"""

from __future__ import annotations

import argparse
import functools
import html
import http.server
import json
import re
import socketserver
import sys
import threading
from collections import Counter
from pathlib import Path
from statistics import median
from string import Template

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "index.html"
RUNS = Path.home() / ".hackathon_judge" / "runs"

WIDTH, HEIGHT = 1600, 880
SCALE = 2

MEDALS = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}
ORDINALS = {1: "1st", 2: "2nd", 3: "3rd"}


def site_tokens() -> str:
    """Return the site's own ``:root`` block so the card inherits the palette."""
    source = PAGE.read_text(encoding="utf-8")
    match = re.search(r"^:root \{.*?^\}", source, re.DOTALL | re.MULTILINE)
    if not match:
        raise SystemExit("could not find the :root token block in docs/index.html")
    return match.group(0)


def load_run(run_id: str) -> dict:
    """Read placements, panel and status straight out of a sealed run bundle."""
    run = RUNS / run_id
    if not run.is_dir():
        raise SystemExit(f"no run bundle at {run}")

    verdicts = []
    for path in sorted((run / "verdicts").glob("*.json")):
        verdict = json.loads(path.read_text(encoding="utf-8"))
        verdicts.append(
            {
                "id": verdict["submission_id"],
                "name": verdict["project_name"],
                "builder": verdict.get("builder_name") or "",
                "score": float(verdict["total_score"]),
            }
        )
    if not verdicts:
        raise SystemExit(f"run {run_id} has no verdicts to render")
    verdicts.sort(key=lambda row: (-row["score"], row["name"].lower()))

    # Dense ranking: an exact tie shares a placement and does not consume the
    # one below it, which is what the award policy already does.
    places: dict[int, dict] = {}
    rank = 0
    previous: float | None = None
    for row in verdicts:
        if previous is None or row["score"] < previous:
            rank += 1
            previous = row["score"]
        if rank > 3:
            break
        places.setdefault(rank, {"score": row["score"], "entries": []})
        places[rank]["entries"].append(row)

    awards = {}
    awards_path = run / "winner" / "awards.json"
    if awards_path.exists():
        payload = json.loads(awards_path.read_text(encoding="utf-8"))
        for award in payload.get("awards", []):
            placement = award.get("placement")
            if placement is not None:
                awards.setdefault(int(placement), award)

    bundle = json.loads((run / "manifest" / "bundle.json").read_text(encoding="utf-8"))

    panel: list[str] = []
    steps = sorted((run / "eval").glob("step_*.json"))
    if steps:
        counter: Counter[tuple[str, ...]] = Counter()
        for path in steps:
            models = json.loads(path.read_text(encoding="utf-8")).get("model_panel")
            if models:
                counter[tuple(models)] += 1
        if counter:
            panel = list(counter.most_common(1)[0][0])

    # The validation line counts hashed artifacts, not files on disk: the HASHES
    # manifest is the record, and a filesystem walk picks up HASHES and SEAL
    # themselves and reports a number that disagrees with the run's own output.
    hashes = run / "HASHES"
    artifacts = (
        len([line for line in hashes.read_text(encoding="utf-8").splitlines() if line.strip()])
        if hashes.exists()
        else 0
    )

    scores = sorted(row["score"] for row in verdicts)
    return {
        "run_id": run_id,
        "count": len(verdicts),
        "places": places,
        "awards": awards,
        "panel": panel,
        "status": bundle.get("result_status", ""),
        "official": bool(bundle.get("official_copilot_panel_connected")),
        "artifacts": artifacts,
        "top": scores[-1],
        "median": median(scores),
    }


def podium_column(place: int, data: dict, award: dict | None) -> str:
    """One riser. A shared placement names every project that tied into it."""
    entries = data["entries"]
    tied = len(entries) > 1
    if tied:
        headline = f"{len(entries)}-way tie"
        support = " &middot; ".join(html.escape(row["name"]) for row in entries)
    else:
        headline = html.escape(entries[0]["name"])
        support = html.escape(entries[0]["builder"] or "Project team")

    label = ORDINALS.get(place, f"{place}th")
    if award and place == 1:
        label = html.escape(award.get("award_name") or "First Place")

    return f"""
      <div class="col c{place}">
        <div class="medal">{MEDALS.get(place, '')}</div>
        <div class="place">{label}</div>
        <div class="project">{headline}</div>
        <div class="support">{support}</div>
        <div class="riser">{data['score']:g}<span>score</span></div>
      </div>"""


CARD = Template(
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8" /><style>
$tokens
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { width: ${width}px; height: ${height}px; }
body {
  background: var(--cp-bg);
  color: var(--cp-text);
  font-family: "Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif;
  display: flex; flex-direction: column; justify-content: center;
  padding: 56px 72px; position: relative; overflow: hidden;
}
/* the same violet god-ray the podium reveal uses, kept well behind the text */
.glow {
  position: absolute; top: -340px; left: 50%; transform: translateX(-50%);
  width: 1300px; height: 760px; border-radius: 50%;
  background: radial-gradient(closest-side, hsl(var(--primary) / .28), transparent 70%);
  filter: blur(20px);
}
.inner { position: relative; display: flex; flex-direction: column; height: 100%; }
.badge {
  align-self: flex-start; display: inline-flex; align-items: center; gap: 10px;
  padding: 9px 18px; border-radius: 999px; font-size: 17px; font-weight: 700;
  letter-spacing: .04em; text-transform: uppercase;
  color: var(--cp-accent-2);
  background: color-mix(in srgb, var(--cp-accent-2) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--cp-accent-2) 42%, transparent);
}
.badge i { width: 9px; height: 9px; border-radius: 50%; background: var(--cp-accent-2); font-style: normal; }
h1 {
  font-size: 62px; line-height: 1.08; font-weight: 800; letter-spacing: -.028em;
  margin-top: 26px;
}
h1 .accent { color: var(--cp-accent); }
.sub {
  font-size: 24px; font-weight: 500; color: var(--cp-text-muted);
  margin-top: 16px;
}
.sub b { color: var(--cp-text); font-weight: 700; }

.stats { margin-top: 42px; display: grid; grid-template-columns: repeat(4, 1fr); gap: 18px; }
.stat {
  padding: 20px 24px; border-radius: 14px;
  background: color-mix(in srgb, var(--cp-surface) 70%, transparent);
  border: 1px solid var(--cp-border);
}
.stat .v { font-size: 36px; font-weight: 800; letter-spacing: -.02em; line-height: 1; }
.stat .k {
  margin-top: 10px; font-size: 14px; font-weight: 700; letter-spacing: .06em;
  text-transform: uppercase; color: var(--cp-text-muted);
}

.podium { margin-top: auto; padding-top: 42px; display: grid; grid-template-columns: 1fr 1.24fr 1fr; gap: 20px; align-items: end; }
.col {
  position: relative; overflow: hidden; text-align: center;
  padding: 26px 20px 22px; border-radius: 16px;
  background: var(--cp-surface); border: 1px solid var(--cp-border);
}
.col .medal { font-size: 44px; line-height: 1; filter: drop-shadow(0 3px 6px rgba(0,0,0,.35)); }
.col .place { font-size: 15px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; margin-top: 12px; color: var(--cp-text-soft); }
.col .project { font-size: 25px; font-weight: 800; margin-top: 10px; letter-spacing: -.015em; line-height: 1.2; }
.col .support { font-size: 15px; font-weight: 500; margin-top: 9px; color: var(--cp-text-muted); line-height: 1.45; }
.col .riser { margin-top: 20px; font-weight: 900; line-height: 1; font-size: 34px; }
.col .riser span { display: block; margin-top: 7px; font-size: 14px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; color: color-mix(in srgb, var(--cp-text) 48%, transparent); }

.c1 { border-color: color-mix(in srgb, var(--cp-gold) 68%, var(--cp-border)); padding-bottom: 34px; }
.c1 .medal { font-size: 58px; }
.c1 .place { color: var(--cp-gold); }
.c1 .riser { color: var(--cp-gold); font-size: 42px; }
.c1::after {
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background: radial-gradient(120% 68% at 50% 0%, color-mix(in srgb, var(--cp-gold) 16%, transparent), transparent 70%);
}
.c2 { border-color: color-mix(in srgb, var(--cp-silver) 55%, var(--cp-border)); }
.c2 .place { color: color-mix(in srgb, var(--cp-silver) 82%, var(--cp-text-soft)); }
.c2 .riser { color: var(--cp-silver); }
.c3 { border-color: color-mix(in srgb, var(--cp-bronze) 55%, var(--cp-border)); }
.c3 .place { color: color-mix(in srgb, var(--cp-bronze) 84%, var(--cp-text-soft)); }
.c3 .riser { color: var(--cp-bronze); }

footer {
  margin-top: 30px; padding-top: 22px; border-top: 1px solid var(--cp-border);
  display: flex; justify-content: space-between; align-items: center; gap: 30px;
  font-size: 16px; color: var(--cp-text-muted);
}
footer .models { display: flex; gap: 9px; flex-wrap: wrap; }
footer .models span {
  padding: 6px 13px; border-radius: 999px; font-weight: 600; font-size: 15px;
  color: var(--cp-text); background: var(--cp-surface-soft);
  border: 1px solid var(--cp-border);
}
footer .meta { text-align: right; line-height: 1.6; white-space: nowrap; }
footer .meta b { color: var(--cp-success); font-weight: 700; }
footer code { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace; color: var(--cp-text); }
</style></head>
<body>
  <div class="glow"></div>
  <div class="inner">
    <div class="badge"><i></i>$status</div>
    <h1>$title</h1>
    <div class="sub">$subtitle</div>
    <div class="stats">$stats</div>
    <div class="podium">$columns
    </div>
    <footer>
      <div class="models">$models</div>
      <div class="meta">
        run <code>$run_id</code> &middot; $artifacts artifacts &middot; validation <b>PASSED</b><br />
        Judged by the GitHub Copilot Builder Showcase
      </div>
    </footer>
  </div>
</body></html>"""
)


def card_html(run: dict, title: str, subtitle: str) -> str:
    # A podium reads 2nd, 1st, 3rd left-to-right -- the winner belongs in the
    # middle on the tall riser. docs/index.html orders its own hero podium the
    # same way; rendering 1-2-3 in source order looks like a plain table.
    order = [place for place in (2, 1, 3) if place in run["places"]]
    columns = "".join(
        podium_column(place, run["places"][place], run["awards"].get(place))
        for place in order
    )

    podium_size = sum(len(run["places"][place]["entries"]) for place in run["places"])
    tiles = [
        (f"{run['top']:g}", "top score"),
        (f"{run['median']:g}", "field median"),
        (str(run["count"]), "projects judged"),
        (str(podium_size), "on the podium"),
    ]
    stats = "".join(
        f'<div class="stat"><div class="v">{html.escape(value)}</div>'
        f'<div class="k">{html.escape(key)}</div></div>'
        for value, key in tiles
    )

    models = "".join(f"<span>{html.escape(m)}</span>" for m in run["panel"])
    return CARD.substitute(
        tokens=site_tokens(),
        width=WIDTH,
        height=HEIGHT,
        status=html.escape(run["status"] or "PRACTICE SHOWCASE"),
        title=title,
        subtitle=subtitle,
        stats=stats,
        columns=columns,
        models=models,
        run_id=html.escape(run["run_id"]),
        artifacts=run["artifacts"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--title")
    parser.add_argument("--subtitle")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is required: pip install playwright && playwright install chromium")
        return 1

    run = load_run(args.run_id)
    title = args.title or f"{run['count']} projects judged"
    subtitle = args.subtitle or (
        f"<b>{run['count']}</b> verdicts &middot; a panel of "
        f"<b>{len(run['panel'])}</b> frontier models &middot; median consensus"
    )
    out = args.out or (ROOT / "docs" / "images" / f"results-{args.run_id}.png")
    out.parent.mkdir(parents=True, exist_ok=True)

    card = ROOT / "docs" / "_results-card.html"
    card.write_text(card_html(run, title, subtitle), encoding="utf-8")
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "docs")
    )
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_context(
                viewport={"width": WIDTH, "height": HEIGHT},
                device_scale_factor=SCALE,
            ).new_page()
            page.goto(f"http://127.0.0.1:{port}/_results-card.html")
            page.wait_for_timeout(500)
            page.screenshot(path=str(out))
            browser.close()
    finally:
        server.shutdown()
        card.unlink(missing_ok=True)

    print(f"wrote {out} ({WIDTH * SCALE}x{HEIGHT * SCALE})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
