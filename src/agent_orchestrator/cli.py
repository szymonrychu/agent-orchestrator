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


# ---------------------------------------------------------------------- service commands
# Imports are lazy so `orch --help` and `orch migrate` load only what they run.


def _settings():
    from agent_orchestrator.runtime.run import configure_logging
    from agent_orchestrator.settings import Settings, SettingsError

    try:
        settings = Settings()
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from None
    configure_logging(settings.log_level)
    return settings


def build_uvicorn_config(settings):
    """uvicorn config for `orch api`.

    Proxy headers are off and no forwarded-allow-ips is set, so the direct TCP peer is what
    the source allowlist sees and X-Forwarded-For cannot spoof it (D22).
    """
    import uvicorn

    from agent_orchestrator.api.app import create_app

    return uvicorn.Config(
        create_app(settings),
        host=settings.http_host,
        port=settings.http_port,
        proxy_headers=False,
        forwarded_allow_ips=None,
        log_config=None,
        timeout_graceful_shutdown=int(settings.shutdown_grace_seconds),
    )


def _cmd_migrate(_args: argparse.Namespace) -> int:
    import logging

    from agent_orchestrator.db.migrate import run_migrations

    settings = _settings()
    applied = run_migrations(settings.database_url)
    logging.getLogger("agent_orchestrator").info("migrations applied: %s", applied or "none")
    return 0


def _cmd_api(_args: argparse.Namespace) -> int:
    import asyncio
    import contextlib

    import uvicorn

    from agent_orchestrator.runtime.run import install_signal_handlers

    class _Server(uvicorn.Server):
        # uvicorn re-raises the captured signal after a graceful stop, which would turn a clean
        # SIGTERM into a signal death; our handlers set should_exit and the exit code stays 0.
        def capture_signals(self):
            return contextlib.nullcontext()

    async def main() -> None:
        server = _Server(build_uvicorn_config(settings))
        stop = asyncio.Event()
        install_signal_handlers(stop)

        async def watch() -> None:
            await stop.wait()
            server.should_exit = True

        watcher = asyncio.create_task(watch())
        try:
            await server.serve()
        finally:
            watcher.cancel()

    settings = _settings()
    asyncio.run(main())
    return 0


def _run(factory_builder, singleton: str | None) -> int:
    import asyncio

    from agent_orchestrator.runtime.run import run_service

    settings = _settings()
    return asyncio.run(run_service(settings, factory_builder(settings), singleton=singleton))


def _cmd_dispatcher(_args: argparse.Namespace) -> int:
    from agent_orchestrator.dispatcher.loop import Dispatcher

    return _run(lambda s: lambda pool: Dispatcher(pool, s), None)


def _cmd_ingester(_args: argparse.Namespace) -> int:
    from agent_orchestrator.ingester.loop import Ingester

    return _run(lambda s: lambda pool: Ingester(pool, s), "ingester")


def _cmd_reconciler(_args: argparse.Namespace) -> int:
    from agent_orchestrator.k8s.kube import LightkubeApi
    from agent_orchestrator.reconciler.loop import Reconciler
    from agent_orchestrator.wrapper.client import WrapperClient

    def build(settings):
        def factory(pool):
            return Reconciler(
                pool,
                LightkubeApi(),
                settings,
                lambda url, token: WrapperClient(
                    url, token, send_timeout=settings.wrapper_send_timeout_seconds
                ),
            )

        return factory

    return _run(build, "reconciler")


register("migrate", _cmd_migrate, "apply database migrations and exit")
register("api", _cmd_api, "serve the HTTP API and UI")
register("dispatcher", _cmd_dispatcher, "dispatch queued messages to agents")
register("ingester", _cmd_ingester, "store every wrapper event (singleton)")
register("reconciler", _cmd_reconciler, "reconcile cluster objects in Kubernetes (singleton)")


if __name__ == "__main__":
    raise SystemExit(main())
