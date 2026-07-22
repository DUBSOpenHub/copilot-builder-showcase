#!/usr/bin/env python3
"""Beginner-first launcher for the Hackathon Judge Live Show."""

from __future__ import annotations

import sys
from typing import Callable, Iterable, List, Optional, TextIO

from hackathon_judge import main as judge_main


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
Hackathon Judge

Start a show:
  hackathon owner/project-one owner/project-two
  hackathon --file submissions.txt

Try the built-in practice show:
  hackathon --demo

Check setup:
  hackathon doctor

Paste only project links to begin. Team names and extra details are optional.
"""


def route_args(argv: List[str]) -> Optional[List[str]]:
    """Route beginner input to the Live Show while preserving advanced commands."""
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
    print("Paste GitHub project links, one per line.", file=output)
    print("Press Return on an empty line to start the show.", file=output)
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
            print("No project links received. Run `hackathon --demo` to try a practice show.", file=output)
            return 7

    routed = route_args(user_args)
    if routed is None:
        print(BEGINNER_HELP, file=output)
        return 0
    return judge_main(routed)


if __name__ == "__main__":
    raise SystemExit(main())
