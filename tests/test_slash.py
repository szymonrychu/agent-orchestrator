"""The slash rule (design D2): the same case table as the wrapper's turn guard tests
(claude-code-wrapper tests/test_session_turns.py, test_slash_rule_matches_claude_parser and
test_slash_rule_leaves_paths_and_prose), case for case, so the two sides cannot drift."""

import pytest

from agents_orchestrator.slash import parse

LONG_NAME = "a" * 100

COMMANDS = [
    ("/reload-plugins", ("reload-plugins", "")),
    ("  /compact keep the plan", ("compact", "keep the plan")),
    ("/workbench:start-development x", ("workbench:start-development", "x")),
    ("/model sonnet", ("model", "sonnet")),
    ("/_x", ("_x", "")),
    ("/tmp", ("tmp", "")),
    (f"/{LONG_NAME}", (LONG_NAME, "")),
    ("/x a\nb", ("x", "a\nb")),
    ("/x\ta", ("x", "a")),
    ("/x\na", ("x", "a")),
]

EMPTY_NAME = [
    ("/", ("", "")),
    ("/ is root", ("", "is root")),
]

PROSE = [
    "please compact the session",
    "/workdir/foo is broken",
    "/etc/hosts",
    "//x",
    "/notes.txt",
    "see /compact",
    "",
]

MARKED = [
    ("/review-pr 12\n\n<!-- agents-webhook:42 -->", ("review-pr", "12")),
    ("/compact\n\n<!-- agents-schedule:7 -->", ("compact", "")),
    (
        "/x <!-- agents-webhook:42 --> keep",
        ("x", "<!-- agents-webhook:42 --> keep"),
    ),
    (
        "/x <!-- agents-webhook:42 -->\n\nmore",
        ("x", "<!-- agents-webhook:42 -->\n\nmore"),
    ),
]


@pytest.mark.parametrize(("text", "expected"), COMMANDS)
def test_parse_slash_commands(text: str, expected: tuple[str, str]) -> None:
    assert parse(text) == expected


@pytest.mark.parametrize(("text", "expected"), EMPTY_NAME)
def test_parse_empty_name_is_command(text: str, expected: tuple[str, str]) -> None:
    assert parse(text) == expected


@pytest.mark.parametrize("text", PROSE)
def test_parse_leaves_prose(text: str) -> None:
    assert parse(text) is None


@pytest.mark.parametrize(("text", "expected"), MARKED)
def test_parse_strips_trailing_run_marker(text: str, expected: tuple[str, str]) -> None:
    assert parse(text) == expected


def test_parse_marker_on_prose_stays_prose() -> None:
    assert parse("/etc/hosts\n\n<!-- agents-webhook:42 -->") is None
    assert parse("hello\n\n<!-- agents-matrix:abc-1 -->") is None
