import io
import sys
from unittest.mock import patch

import showcase_launcher as launcher


class TtyInput(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_routes_links_and_flags_to_live_show():
    assert launcher.route_args(["owner/project"]) == ["workshop", "owner/project"]
    assert launcher.route_args(["--demo"]) == ["workshop", "--demo"]


def test_preserves_advanced_commands_and_version():
    assert launcher.route_args(["doctor"]) == ["doctor"]
    assert launcher.route_args(["replay", "show-1"]) == ["replay", "show-1"]
    assert launcher.route_args(["--version"]) == ["--version"]


def test_help_is_beginner_facing():
    output = io.StringIO()

    assert launcher.main(["--help"], output=output) == 0

    help_text = output.getvalue()
    assert "showcase owner/project-one owner/project-two" in help_text
    assert "showcase --demo" in help_text
    assert "workshop" not in help_text


def test_empty_command_collects_pasted_links():
    answers = iter(["owner/one", "https://github.com/owner/two", ""])
    output = io.StringIO()

    with patch.object(sys, "stdin", TtyInput()), patch.object(
        launcher, "showcase_main", return_value=0
    ) as showcase_main:
        assert launcher.main([], input_fn=lambda _: next(answers), output=output) == 0

    showcase_main.assert_called_once_with(
        ["workshop", "owner/one", "https://github.com/owner/two"]
    )
    assert "Paste project or demo links" in output.getvalue()


def test_piped_links_start_the_show():
    output = io.StringIO()

    with patch.object(sys, "stdin", io.StringIO("owner/one\nowner/two\n")), patch.object(
        launcher, "showcase_main", return_value=0
    ) as showcase_main:
        assert launcher.main([], output=output) == 0

    showcase_main.assert_called_once_with(["workshop", "owner/one", "owner/two"])


def test_empty_noninteractive_input_gives_demo_hint():
    output = io.StringIO()

    with patch.object(sys, "stdin", io.StringIO("")):
        assert launcher.main([], output=output) == 7

    assert "showcase --demo" in output.getvalue()
