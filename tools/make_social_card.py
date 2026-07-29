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
OUT = ROOT / "docs" / "images" / "social-card-v2.png"
# Slack, Teams and LinkedIn cache a page's preview by image URL, so a redesign
# only reaches people who have never shared the link unless the filename
# changes. But the previous filename cannot simply disappear either: clients
# holding the old page still request it, and a 404 shows them no image at all.
# Every past filename keeps working and serves the current artwork.
LEGACY_OUT = [ROOT / "docs" / "images" / "social-card.png"]

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
.inner {{ position: relative; }}
/* Line breaks are explicit. This renders at one fixed size and never reflows,
   and the name has to land whole on its own line rather than wherever the
   text happens to wrap. */
h1 {{
  font-size: 84px; line-height: 1.1; font-weight: 800; letter-spacing: -.028em;
  white-space: nowrap;
}}
h1 .accent {{ color: var(--cp-accent); }}
/* The headline already says GitHub Copilot, so the subline does not repeat it. */
p {{
  font-size: 29px; line-height: 1.45; font-weight: 500;
  color: var(--cp-text-muted); margin-top: 40px; max-width: 940px;
}}
</style></head>
<body>
  <div class="glow"></div>
  <div class="inner">
    <h1>End every workshop<br />with a <span class="accent">GitHub Copilot</span><br />Builder Showcase</h1>
    <p>Drop the links, activate the judging panel, and spotlight the winners &mdash; in under two minutes.</p>
  </div>
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

    for legacy in LEGACY_OUT:
        legacy.write_bytes(OUT.read_bytes())

    written = ", ".join(
        str(p.relative_to(ROOT)) for p in [OUT, *LEGACY_OUT]
    )
    print(f"wrote {written} ({WIDTH}x{HEIGHT})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
