"""A stale install has to say so.

The `showcase` command runs from ~/.local/share/copilot-builder-showcase, a
separate checkout from this repo, so it can sit several versions behind with
nothing anywhere to say so — it silently ran three releases old for months.

The check is advisory only. A setup check must never fail, hang, or leak a
stack trace because GitHub was briefly unreachable, and the test suite must
never reach the network at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import builder_showcase as cbp


def release_payload(tag: str):
    def fetch(url: str, timeout: float) -> str:
        assert "releases/latest" in url
        assert timeout <= 5, "a setup check must not hang on a slow network"
        return json.dumps({"tag_name": tag})

    return fetch


class RecordingFetch:
    """The lookup swallows every exception, so a raise alone proves nothing."""

    def __init__(self, tag: str = "v99.0.0"):
        self.called = False
        self.tag = tag

    def __call__(self, url: str, timeout: float) -> str:
        self.called = True
        return json.dumps({"tag_name": self.tag})


class TestVersionComparison:
    @pytest.mark.parametrize(
        "left,right",
        [
            ("3.4.1", "3.4.2"),
            ("3.4.1", "3.5.0"),
            ("3.9.9", "4.0.0"),
            ("v3.4.1", "v3.4.10"),
        ],
    )
    def test_ordering(self, left, right):
        assert cbp._version_tuple(left) < cbp._version_tuple(right)

    def test_a_leading_v_is_ignored(self):
        assert cbp._version_tuple("v3.4.1") == cbp._version_tuple("3.4.1")

    def test_ten_sorts_above_nine_not_between_one_and_two(self):
        """String comparison would put 3.4.10 below 3.4.9."""
        assert cbp._version_tuple("3.4.10") > cbp._version_tuple("3.4.9")

    def test_junk_does_not_raise(self):
        assert cbp._version_tuple("") == ()
        assert cbp._version_tuple(None) == ()
        assert cbp._version_tuple("not-a-version") == ()


class TestUpdateNotice:
    def test_a_newer_release_is_announced(self):
        notice = cbp._update_notice("3.4.1", "v3.4.2")
        assert notice and "3.4.2" in notice and "3.4.1" in notice

    def test_the_current_release_is_silent(self):
        assert cbp._update_notice("3.4.1", "v3.4.1") is None

    def test_an_older_tag_is_silent(self):
        """A yanked release must not tell everyone to downgrade."""
        assert cbp._update_notice("3.4.1", "v3.3.9") is None

    def test_an_unreachable_registry_is_silent(self):
        assert cbp._update_notice("3.4.1", None) is None

    def test_an_unparseable_tag_is_silent(self):
        assert cbp._update_notice("3.4.1", "garbage") is None


class TestLookupFailsOpen:
    def test_a_network_error_returns_nothing_instead_of_raising(self):
        def broken(url, timeout):
            raise OSError("network is unreachable")

        assert cbp._latest_released_version(broken, environ={}) is None

    def test_malformed_json_returns_nothing(self):
        assert cbp._latest_released_version(
            lambda url, timeout: "<html>502</html>", environ={}
        ) is None

    def test_a_missing_tag_returns_nothing(self):
        assert cbp._latest_released_version(
            lambda url, timeout: json.dumps({}), environ={}
        ) is None

    def test_a_good_response_is_read(self):
        assert cbp._latest_released_version(
            release_payload("v9.9.9"), environ={}
        ) == "v9.9.9"


class TestTheSuiteNeverReachesTheNetwork:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
    def test_ci_skips_the_lookup_entirely(self, value):
        fetch = RecordingFetch()
        assert cbp._latest_released_version(fetch, environ={"CI": value}) is None
        assert not fetch.called, "the update check reached the network under CI"

    def test_an_explicit_opt_out_skips_the_lookup(self):
        fetch = RecordingFetch()
        assert cbp._latest_released_version(
            fetch, environ={"SHOWCASE_SKIP_UPDATE_CHECK": "1"}
        ) is None
        assert not fetch.called

    def test_the_real_environment_is_used_by_default(self, monkeypatch):
        monkeypatch.setenv("SHOWCASE_SKIP_UPDATE_CHECK", "1")
        fetch = RecordingFetch()
        assert cbp._latest_released_version(fetch) is None
        assert not fetch.called

    def test_the_lookup_does_run_when_nothing_blocks_it(self):
        """Otherwise the guards above would pass on a check that never works."""
        fetch = RecordingFetch()
        assert cbp._latest_released_version(fetch, environ={}) == "v99.0.0"
        assert fetch.called


class TestDoctorReportsIt:
    def _run(self, capsys, fetch):
        args = argparse.Namespace(run_id=None, _fetch_release=fetch)
        cbp.cmd_doctor(args)
        return capsys.readouterr().out

    def test_doctor_names_the_installed_version(self, capsys, monkeypatch):
        monkeypatch.delenv("CI", raising=False)
        out = self._run(capsys, release_payload(f"v{cbp.VERSION}"))
        assert cbp.VERSION in out

    def test_doctor_warns_when_a_newer_release_exists(self, capsys, monkeypatch):
        monkeypatch.delenv("CI", raising=False)
        out = self._run(capsys, release_payload("v99.0.0"))
        assert "Update available" in out
        assert "99.0.0" in out

    def test_doctor_is_quiet_when_current(self, capsys, monkeypatch):
        monkeypatch.delenv("CI", raising=False)
        out = self._run(capsys, release_payload(f"v{cbp.VERSION}"))
        assert "Update available" not in out

    def test_doctor_does_not_claim_current_when_it_never_checked(self, capsys):
        """Under CI the lookup is skipped, so "(current)" would be a guess."""
        args = argparse.Namespace(run_id=None, _fetch_release=RecordingFetch())
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"CI": "true"}):
            cbp.cmd_doctor(args)
        out = capsys.readouterr().out
        assert cbp.VERSION in out
        assert "(current)" not in out
        assert "Update available" not in out

    def test_a_stale_install_is_not_an_error(self, capsys, monkeypatch):
        """Being behind still runs a showcase; it must not fail the check."""
        monkeypatch.delenv("CI", raising=False)
        args = argparse.Namespace(
            run_id=None, _fetch_release=release_payload("v99.0.0")
        )
        assert cbp.cmd_doctor(args) == 0

    def test_doctor_survives_an_unreachable_registry(self, capsys, monkeypatch):
        monkeypatch.delenv("CI", raising=False)

        def broken(url, timeout):
            raise OSError("network is unreachable")

        args = argparse.Namespace(run_id=None, _fetch_release=broken)
        assert cbp.cmd_doctor(args) == 0
        assert cbp.VERSION in capsys.readouterr().out


class TestTokenReuse:
    """60 requests/hour per IP is thin for a workshop room behind one NAT."""

    def test_a_token_already_in_the_environment_is_used(self):
        assert cbp._release_api_token({"GITHUB_TOKEN": "ghp_x"}) == "ghp_x"
        assert cbp._release_api_token({"GH_TOKEN": "ghp_y"}) == "ghp_y"

    def test_github_token_wins_over_gh_token(self):
        assert cbp._release_api_token(
            {"GITHUB_TOKEN": "a", "GH_TOKEN": "b"}
        ) == "a"

    def test_no_token_is_fine(self):
        assert cbp._release_api_token({}) is None
        assert cbp._release_api_token({"GITHUB_TOKEN": "   "}) is None

    def test_the_token_is_never_printed(self, capsys, monkeypatch):
        """A leaked credential in terminal scrollback is a screen-share risk."""
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecretvalue")
        args = argparse.Namespace(
            run_id=None, _fetch_release=release_payload(f"v{cbp.VERSION}")
        )
        cbp.cmd_doctor(args)
        captured = capsys.readouterr()
        assert "ghp_supersecretvalue" not in captured.out
        assert "ghp_supersecretvalue" not in captured.err


class TestVersionParity:
    def test_the_package_and_the_engine_agree(self):
        """
        The staleness check compares VERSION against the published tag, so a
        drifted pyproject would have pip and doctor reporting different
        versions and the comparison answering for the wrong one.
        """
        import tomllib

        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        assert declared["project"]["version"] == cbp.VERSION
