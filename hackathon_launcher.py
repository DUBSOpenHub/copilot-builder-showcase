#!/usr/bin/env python3
"""Compatibility entry point for the legacy `hackathon` command."""

from showcase_launcher import *  # noqa: F401,F403
from showcase_launcher import main


if __name__ == "__main__":
    raise SystemExit(main())
