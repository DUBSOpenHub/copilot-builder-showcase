"""The link preview has to survive being shared.

A stale ``og:image`` is invisible locally and only shows up once someone posts
the link in Slack or LinkedIn, so it is worth pinning. These caught a real
break: the tags advertised 1280x900 while the file had been regenerated at
1431x1044, and at that 1.37:1 shape every platform cropped the card to 1.91:1,
cutting the headline in half.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "index.html"
SITE_ROOT = "https://dubsopenhub.github.io/copilot-builder-showcase/"


def png_size(path: Path) -> tuple[int, int]:
    """Read width and height straight from the PNG IHDR chunk."""
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", header[16:24])
    return width, height


@pytest.fixture(scope="module")
def meta() -> dict[str, str]:
    source = PAGE.read_text(encoding="utf-8")
    found = {}
    for attr, key, value in re.findall(
        r'<meta (property|name)="([^"]+)" content="([^"]*)"', source
    ):
        found[key] = value
    return found


def _local_path(url: str) -> Path:
    assert url.startswith(SITE_ROOT), f"{url} is not served from the site root"
    return ROOT / "docs" / url[len(SITE_ROOT):]


def test_share_image_exists(meta: dict[str, str]) -> None:
    for key in ("og:image", "twitter:image"):
        assert key in meta, f"{key} is missing"
        assert _local_path(meta[key]).is_file(), f"{key} points at a missing file"


def test_og_and_twitter_share_the_same_image(meta: dict[str, str]) -> None:
    assert meta["og:image"] == meta["twitter:image"]
    assert meta["og:image:alt"] == meta["twitter:image:alt"]


def test_declared_dimensions_match_the_actual_file(meta: dict[str, str]) -> None:
    """The exact drift that shipped: tags said 1280x900, the file was 1431x1044."""
    width, height = png_size(_local_path(meta["og:image"]))
    assert int(meta["og:image:width"]) == width
    assert int(meta["og:image:height"]) == height


def test_share_image_is_shaped_for_a_social_card(meta: dict[str, str]) -> None:
    """Cards are cropped to roughly 1.91:1. A taller image loses its headline."""
    width, height = png_size(_local_path(meta["og:image"]))
    aspect = width / height
    assert 1.85 <= aspect <= 1.95, (
        f"share image is {width}x{height} ({aspect:.2f}:1); platforms will crop it"
    )
    assert width >= 1200, "cards render blurry below 1200px wide"


def test_share_alt_text_does_not_name_a_demo_project(meta: dict[str, str]) -> None:
    """The practice podium is randomized per run, so naming a winner goes stale."""
    demo_projects = [
        "Aurora Recall", "Commit Coach", "Reef Guardian",
        "PixelForge", "LatencyLab", "Signal Garden",
    ]
    for key in ("og:image:alt", "twitter:image:alt"):
        for project in demo_projects:
            assert project not in meta[key], (
                f"{key} names the demo project {project!r}, which changes every run"
            )
