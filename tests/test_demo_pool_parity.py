"""The big screen has to demo the same projects the engine demos.

``docs/index.html`` carries its own copy of the practice project pool because
GitHub Pages cannot read ``demo_projects.json`` at runtime and the page has to
work offline. Two copies drift, so this pins them together: regenerate both
with ``python3 tools/make_demo_projects.py`` rather than editing either by
hand.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "index.html"
POOL = ROOT / "demo_projects.json"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import builder_showcase as cbp  # noqa: E402
import make_demo_projects  # noqa: E402


@pytest.fixture(scope="module")
def pool() -> dict:
    return json.loads(POOL.read_text(encoding="utf-8"))


def _unescape(value: str) -> str:
    """Undo the escaping the generator applies when it writes the JS array."""
    return value.replace('\\"', '"').replace("\\\\", "\\")


@pytest.fixture(scope="module")
def page_demo() -> list[dict[str, str]]:
    source = PAGE.read_text(encoding="utf-8")
    block = re.search(r"var DEMO = \[(.*?)\n  \];", source, re.DOTALL)
    assert block, "the big screen no longer has a DEMO array"
    entries = []
    for row in re.finditer(
        r'\{ proj: "(.*?)", link: "(.*?)", team: "(.*?)", desc: "(.*?)" \}',
        block.group(1),
    ):
        entries.append(
            {
                "proj": _unescape(row.group(1)),
                "link": _unescape(row.group(2)),
                "team": _unescape(row.group(3)),
                "desc": _unescape(row.group(4)),
            }
        )
    return entries


def test_the_page_and_the_engine_show_the_same_projects(pool, page_demo):
    assert [e["link"] for e in page_demo] == [p["url"] for p in pool["projects"]]


def test_the_page_and_the_engine_agree_on_attribution(pool, page_demo):
    assert [e["team"] for e in page_demo] == [
        p["builder_name"] for p in pool["projects"]
    ]


def test_page_descriptions_are_the_shortened_pool_descriptions(pool, page_demo):
    for entry, project in zip(page_demo, pool["projects"]):
        assert entry["desc"] == make_demo_projects.shorten(project["builder_notes"])


def test_both_surfaces_rotate_the_same_slate_size(pool):
    source = PAGE.read_text(encoding="utf-8")
    page_size = re.search(r"var DEMO_SLATE_SIZE = (\d+);", source)
    assert page_size, "the big screen no longer declares a slate size"
    assert int(page_size.group(1)) == pool["rotation_size"] == cbp.DEMO_ROTATION_SIZE


def test_the_pool_needs_more_than_one_slate_to_be_worth_rotating(pool):
    slates = math.ceil(len(pool["projects"]) / pool["rotation_size"])
    assert slates > 1, "a single slate is not a rotation"


def test_no_demo_project_is_listed_twice(pool):
    urls = [p["url"] for p in pool["projects"]]
    assert len(urls) == len(set(urls))
