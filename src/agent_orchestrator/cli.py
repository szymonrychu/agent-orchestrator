"""argparse front end with a command registry."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from agent_orchestrator import __version__

CommandFunc = Callable[[argparse.Namespace], int | None]

_COMMANDS: dict[str, tuple[CommandFunc, str]] = {}


def register(name: str, func: CommandFunc, help: str) -> None:  # noqa: A002
    """Register a subcommand; func receives the parsed args and returns the exit code."""
    _COMMANDS[name] = (func, help)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orch", description="agent-orchestrator")
    parser.add_argument("--version", action="version", version=f"orch {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, (func, help_text) in _COMMANDS.items():
        sub.add_parser(name, help=help_text).set_defaults(func=func)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        raise SystemExit(2)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
