"""`--help` is the documentation an agent reads: every command is listed, has examples, and every option says what it does."""
from __future__ import annotations

import argparse

import pytest

from savetokens import cli


def _parser(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    return capsys.readouterr().out


def test_top_help_groups_every_command_and_shows_the_common_jobs(capsys):
    out = _parser(capsys)
    grouped = [c for _, cmds in cli.GROUPS for c in cmds]
    assert sorted(grouped) == sorted(cli.EXAMPLES) and len(grouped) == len(set(grouped))
    assert all(f"  {c} " in out for c in grouped)
    assert "common jobs:" in out and "server --setup" in out and "{install," not in out


def _subparsers(monkeypatch):
    seen = {}

    def grab(self, args=None, namespace=None):
        seen.update(next(a for a in self._actions if isinstance(a, argparse._SubParsersAction)).choices)
        raise SystemExit(0)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", grab)
    with pytest.raises(SystemExit):
        cli.main(["status"])
    monkeypatch.undo()
    return seen


@pytest.mark.parametrize("name", sorted(cli.EXAMPLES))
def test_each_command_has_examples_and_every_option_is_explained(name, capsys, monkeypatch):
    sub = _subparsers(monkeypatch)[name]
    for action in sub._actions:
        assert action.help, f"{name} {action.option_strings or action.dest} has no help"
    with pytest.raises(SystemExit):
        cli.main([name, "--help"])
    out = capsys.readouterr().out
    assert out.startswith(f"usage: savetokens {name} ") and f"savetokens {name}" in out.split("examples:")[1]
    assert ("--json prints" in out) == (name not in ("watch", "web", "mcp", "server"))
    assert ("  4  " in out) == (name == "check")
