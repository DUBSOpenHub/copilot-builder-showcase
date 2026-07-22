#!/usr/bin/env python3
"""Beginner-first launcher for Copilot Builder Showcase."""

from __future__ import annotations

import sys
from typing import Callable, Iterable, List, Optional, TextIO

from builder_showcase import main as showcase_main


ADVANCED_COMMANDS = frozenset(
    {
        "award",
        "compare",
        "doctor",
        "export",
        "feedback",
        "import-urls",
        "init",
        "judge",
        "list",
        "present",
        "quick",
        "replay",
        "resume",
        "submit",
        "tui",
        "validate",
        "workshop",
    }
)

BEGINNER_HELP = """\
Copilot Builder Showcase

Start a showcase:
  showcase owner/project-one owner/project-two
  showcase --file submissions.txt

Try the built-in practice showcase:
  showcase --demo

Check setup:
  showcase doctor

Paste only project links to begin. Team names and extra details are optional.
"""


def route_args(argv: List[str]) -> Optional[List[str]]:
    """Route beginner input to the live showcase while preserving advanced commands."""
    if argv and argv[0] in {"-h", "--help"}:
        return None
    if argv and (argv[0] in ADVANCED_COMMANDS or argv[0] == "--version"):
        return argv
    return ["workshop", *argv]


def _clean_project_lines(lines: Iterable[str]) -> List[str]:
    return [line.strip() for line in lines if line.strip()]


def collect_project_links(
    input_fn: Optional[Callable[[str], str]] = None,
    output: Optional[TextIO] = None,
) -> List[str]:
    """Collect a paste-friendly list of project links from a real terminal."""
    input_fn = input_fn or input
    output = output or sys.stdout
    print("Paste project or demo links, one per line.", file=output)
    print("Press Return on an empty line to start the showcase.", file=output)
    links: List[str] = []
    while True:
        try:
            line = input_fn("> ").strip()
        except EOFError:
            break
        if not line:
            break
        links.append(line)
    return links


def main(
    argv: Optional[List[str]] = None,
    *,
    input_fn: Optional[Callable[[str], str]] = None,
    output: Optional[TextIO] = None,
) -> int:
    output = output or sys.stdout
    user_args = list(sys.argv[1:] if argv is None else argv)
    if not user_args:
        if sys.stdin.isatty():
            user_args = collect_project_links(input_fn, output)
        else:
            user_args = _clean_project_lines(sys.stdin)
        if not user_args:
            print(
                "No project links received. Run `showcase --demo` to try a practice showcase.",
                file=output,
            )
            return 7

    routed = route_args(user_args)
    if routed is None:
        print(BEGINNER_HELP, file=output)
        return 0
    return showcase_main(routed)


if __name__ == "__main__":
    raise SystemExit(main())
