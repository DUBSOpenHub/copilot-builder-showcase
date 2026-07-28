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
h1 {{
  font-size: 82px; line-height: 1.04; font-weight: 800; letter-spacing: -.024em;
  max-width: 1030px;
}}
h1 .accent {{ color: var(--cp-accent); }}
p {{
  font-size: 32px; line-height: 1.4; font-weight: 500; color: var(--cp-text);
  margin-top: 30px; max-width: 1000px;
}}
p b {{ color: var(--cp-accent); font-weight: 700; }}
</style></head>
<body>
  <div class="glow"></div>
  <div class="inner">
    <h1>Turn any workshop into a <span class="accent">live showcase</span>.</h1>
    <p>Drop the links, let a <b>GitHub Copilot</b> panel judge every project, and spotlight the winners &mdash; in under two minutes.</p>
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

    print(f"wrote {OUT.relative_to(ROOT)} ({WIDTH}x{HEIGHT})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
