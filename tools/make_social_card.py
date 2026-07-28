#!/usr/bin/env python3
"""Render the 1200x630 social card served as og:image.

The podium screenshot used to do this job, and it did it badly: at 1.37:1 every
platform cropped it to 1.91:1, which cut the headline in half and truncated the
award cards mid-sentence, and its body text was far too small to read at feed
size anyway. This renders a card built for that size instead.

Design tokens are lifted out of ``docs/index.html`` at render time rather than
copied here, so the card cannot drift away from the site's own palette.

Usage:
    python3 tools/make_social_card.py

Requires Playwright (``pip install playwright && playwright install chromium``).
"""

from __future__ import annotations

import functools
import http.server
import re
import socketserver
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "index.html"
OUT = ROOT / "docs" / "images" / "social-card.png"

WIDTH, HEIGHT = 1200, 630


def site_tokens() -> str:
    """Return the site's own ``:root`` block so the card inherits the palette."""
    source = PAGE.read_text(encoding="utf-8")
    match = re.search(r"^:root \{.*?^\}", source, re.DOTALL | re.MULTILINE)
    if not match:
        raise SystemExit("could not find the :root token block in docs/index.html")
    return match.group(0)


def card_html() -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" /><style>
{site_tokens()}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {WIDTH}px; height: {HEIGHT}px; }}
body {{
  background: var(--cp-bg);
  color: var(--cp-text);
  font-family: "Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif;
  display: flex; flex-direction: column; justify-content: center;
  padding: 0 72px; position: relative; overflow: hidden;
}}
/* the same violet god-ray the podium reveal uses, kept well behind the text */
.glow {{
  position: absolute; top: -320px; left: 50%; transform: translateX(-50%);
  width: 1100px; height: 700px; border-radius: 50%;
  background: radial-gradient(closest-side, hsl(var(--primary) / .30), transparent 70%);
  filter: blur(20px);
}}
.glow-warm {{
  position: absolute; bottom: -300px; left: 50%; transform: translateX(-50%);
  width: 900px; height: 520px; border-radius: 50%;
  background: radial-gradient(closest-side, rgba(240,180,41,.16), transparent 70%);
}}
.inner {{ position: relative; }}
.pill {{
  display: inline-flex; align-items: center; gap: 10px;
  font-size: 20px; font-weight: 800; letter-spacing: .14em; text-transform: uppercase;
  color: var(--cp-accent); background: var(--cp-accent-soft);
  border: 1px solid hsl(var(--primary) / .45); border-radius: 999px; padding: 10px 22px;
}}
h1 {{
  font-size: 66px; line-height: 1.06; font-weight: 800; letter-spacing: -.022em;
  margin-top: 26px; max-width: 1010px;
}}
h1 .accent {{ color: var(--cp-accent); }}
p {{
  font-size: 27px; line-height: 1.42; color: var(--cp-text-muted);
  margin-top: 20px; max-width: 990px;
}}
p b {{ color: var(--cp-text); font-weight: 700; }}
.podium {{ display: flex; gap: 14px; margin-top: 36px; }}
.chip {{
  display: inline-flex; align-items: center; gap: 11px;
  font-size: 22px; font-weight: 700; color: var(--cp-text);
  background: var(--cp-surface); border: 1px solid var(--cp-border);
  border-radius: 999px; padding: 13px 24px;
}}
.chip.win {{ border-color: rgba(240,180,41,.55); background: rgba(240,180,41,.10); }}
.chip em {{ font-style: normal; color: var(--cp-text-muted); font-weight: 600; }}
.url {{
  position: absolute; right: 72px; bottom: 40px;
  font-size: 21px; font-weight: 700; color: var(--cp-text-muted);
}}
</style></head>
<body>
  <div class="glow"></div><div class="glow-warm"></div>
  <div class="inner">
    <span class="pill">GitHub Copilot Builder Showcase</span>
    <h1>Turn any workshop into a <span class="accent">live showcase</span>.</h1>
    <p>Drop the links, let a <b>GitHub Copilot</b> panel judge every project, and spotlight the winners &mdash; in under two minutes.</p>
    <div class="podium">
      <span class="chip win">&#127942; GitHub Copilot Builder Award</span>
      <span class="chip">&#129352; <em>Builder Silver</em></span>
      <span class="chip">&#129353; <em>Builder Bronze</em></span>
    </div>
  </div>
  <span class="url">dubsopenhub.github.io/copilot-builder-showcase</span>
</body></html>"""


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is required: pip install playwright && playwright install chromium")
        return 1

    card = ROOT / "docs" / "_social-card.html"
    card.write_text(card_html(), encoding="utf-8")
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
                viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1
            ).new_page()
            page.goto(f"http://127.0.0.1:{port}/_social-card.html")
            page.wait_for_timeout(500)
            page.screenshot(path=str(OUT))
            browser.close()
    finally:
        server.shutdown()
        card.unlink(missing_ok=True)

    print(f"wrote {OUT.relative_to(ROOT)} ({WIDTH}x{HEIGHT})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
