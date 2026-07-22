#!/usr/bin/env python3
"""Compatibility entry point for Copilot Builder Showcase."""

import builder_showcase as _implementation

globals().update(
    {
        name: getattr(_implementation, name)
        for name in dir(_implementation)
        if not name.startswith("__")
    }
)

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
