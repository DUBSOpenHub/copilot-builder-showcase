#!/usr/bin/env python3
"""Refresh the bundled practice-showcase project pool.

The demo ships real, public GitHub projects rather than invented ones, so the
practice run looks like a real event. Every field here comes from the public
GitHub API at generation time — never from a curated description elsewhere —
so the committed pool can always be reproduced from public data.

Evidence is deliberately conservative. ``copilot_evidence`` and
``frontier_evidence`` are only set when the *stored* description and topics say
so, because the engine must never infer either claim. The stored topic list is
truncated, so the same truncated view decides the evidence; deciding from the
full topic list would write a claim the shipped metadata cannot support.

The pool is written into ``builder_showcase.py`` and mirrored into
``docs/index.html``. It is embedded in the engine rather than kept in a data
file because the Windows installer pip-installs the module and a sibling data
file does not survive that copy.

Usage:
    python3 tools/make_demo_projects.py            # refresh the current pool
    python3 tools/make_demo_projects.py repos.txt  # rebuild from owner/repo lines

Requires the GitHub CLI (``gh``) to be installed and authenticated.
"""

from __future__ import annotations

import hashlib
import json
import pprint
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "builder_showcase.py"
PAGE = ROOT / "docs" / "index.html"

ROTATION_SIZE = 20
TOPIC_LIMIT = 4
COPILOT_WORDS = ("copilot",)
FRONTIER_WORDS = ("multi-model", "multi-agent", "swarm")


def clean(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fetch(name: str) -> Optional[Dict[str, Any]]:
    """Return public repository metadata, or None if it is not public."""
    result = subprocess.run(
        ["gh", "api", f"repos/{name}"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout)
    if data.get("private"):
        return None
    return data


def build(data: Dict[str, Any]) -> Dict[str, Any]:
    name = data["full_name"]
    owner = name.split("/")[0]
    topics = (data.get("topics") or [])[:TOPIC_LIMIT]
    description = clean(data.get("description"))
    # Only what ships can justify a claim, so read the truncated view.
    stated = f"{description} {' '.join(topics)}".lower()
    return {
        "url": f"https://github.com/{name}",
        "builder_name": f"@{owner}",
        "copilot_evidence": (
            "Public repository description and topics state GitHub Copilot involvement."
            if any(word in stated for word in COPILOT_WORDS)
            else ""
        ),
        "frontier_evidence": (
            "Public repository description and topics state multi-model or multi-agent work."
            if any(word in stated for word in FRONTIER_WORDS)
            else ""
        ),
        "demo_url": clean(data.get("homepage")),
        "builder_notes": description,
        "repo": {
            "name_with_owner": name,
            "description": description,
            "language": data.get("language") or "",
            "stars": data.get("stargazers_count") or 0,
            "forks": data.get("forks_count") or 0,
            "updated_at": data.get("updated_at") or "",
            "pushed_at": data.get("pushed_at") or "",
            "topics": topics,
            "homepage": clean(data.get("homepage")),
        },
    }


def source_repos(argv: List[str]) -> List[str]:
    if len(argv) > 1:
        lines = Path(argv[1]).read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip()]
    sys.path.insert(0, str(ROOT))
    import builder_showcase

    return [p["repo"]["name_with_owner"] for p in builder_showcase.DEMO_POOL]


def js_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def shorten(value: str, limit: int = 92) -> str:
    """Keep a card description to one readable line on a projector."""
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip(" ,.;:-") + "\u2026"


def embed_in_engine(projects: List[Dict[str, Any]]) -> None:
    """Rewrite the engine's embedded pool between its generated markers."""
    rows = []
    for project in projects:
        body = pprint.pformat(project, width=84, sort_dicts=False, indent=1)
        rows.append("\n".join(("    " + line) for line in body.splitlines()) + ",")
    block = (
        "# --- BEGIN GENERATED DEMO POOL --- (tools/make_demo_projects.py)\n"
        "DEMO_POOL: List[Dict[str, Any]] = [\n"
        + "\n".join(rows)
        + "\n]\n"
        "# --- END GENERATED DEMO POOL ---\n"
    )
    source = ENGINE.read_text(encoding="utf-8")
    source, sized = re.subn(
        r"^DEMO_ROTATION_SIZE = \d+$",
        f"DEMO_ROTATION_SIZE = {ROTATION_SIZE}",
        source,
        count=1,
        flags=re.MULTILINE,
    )
    if sized != 1:
        raise SystemExit("could not find DEMO_ROTATION_SIZE in builder_showcase.py")
    updated, count = re.subn(
        r"# --- BEGIN GENERATED DEMO POOL ---.*?# --- END GENERATED DEMO POOL ---\n",
        lambda _: block,
        source,
        count=1,
        flags=re.DOTALL,
    )
    if count != 1:
        raise SystemExit("could not find the generated demo pool in builder_showcase.py")
    ENGINE.write_text(updated, encoding="utf-8")


def embed_in_page(projects: List[Dict[str, Any]]) -> None:
    """
    Rewrite the big screen's copy of the pool.

    GitHub Pages cannot read the engine at runtime for an offline practice
    run, so the page carries its own copy. Generating both from here keeps the
    two from drifting; ``tests/test_demo_pool_parity.py`` fails if they ever
    do.
    """
    rows = []
    for project in projects:
        rows.append(
            '    {{ proj: "{proj}", link: "{link}", team: "{team}", desc: "{desc}" }}'.format(
                proj=js_string(project["repo"]["name_with_owner"].split("/")[1]),
                link=js_string(project["url"]),
                team=js_string(project["builder_name"]),
                desc=js_string(shorten(project["builder_notes"])),
            )
        )
    block = "  var DEMO = [\n" + ",\n".join(rows) + "\n  ];\n"
    source = PAGE.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"  var DEMO = \[.*?\n  \];\n", block, source, count=1, flags=re.DOTALL
    )
    if count != 1:
        raise SystemExit("could not find the DEMO array in docs/index.html")
    PAGE.write_text(updated, encoding="utf-8")


def main(argv: List[str]) -> int:
    names = source_repos(argv)
    with ThreadPoolExecutor(max_workers=12) as pool:
        fetched = list(pool.map(fetch, names))

    projects = [build(data) for data in fetched if data]
    # A project with no description makes a spotlight with nothing to say.
    projects = [p for p in projects if p["builder_notes"]]
    # Stable shuffle so one slate mixes owners instead of clustering by name.
    projects.sort(key=lambda p: hashlib.sha256(p["url"].encode()).hexdigest())

    embed_in_engine(projects)
    embed_in_page(projects)
    skipped = len(names) - len(projects)
    print(
        f"wrote {ENGINE.relative_to(ROOT)} and {PAGE.relative_to(ROOT)}: "
        f"{len(projects)} projects ({skipped} skipped)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
