"""Tests for x_cli.cli."""

from click.testing import CliRunner

from x_cli.cli import cli


def test_help_short_alias():
    result = CliRunner().invoke(cli, ["-h"])

    assert result.exit_code == 0
    assert "x-cli: CLI for X/Twitter API v2." in result.output
    assert "-h, --help" in result.output
