#!/usr/bin/env python3
"""Compatibility entry point for the legacy optional monitor module."""

from builder_showcase_dashboard import *  # noqa: F401,F403
from builder_showcase_dashboard import main


if __name__ == "__main__":
    raise SystemExit(main())
