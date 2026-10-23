import pytest

from agent_orchestrator import __version__, cli


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    monkeypatch.setattr(cli, "_COMMANDS", {})


def test_version_prints_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_registered_command_dispatches():
    seen = []

    def func(args):
        seen.append(args)
        return 7

    cli.register("hello", func, "say hello")
    assert cli.main(["hello"]) == 7
    assert len(seen) == 1


@pytest.mark.parametrize("argv", [[], ["nope"]])
def test_unknown_command_exits_2(argv, capsys):
    cli.register("hello", lambda args: 0, "say hello")
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "usage" in (captured.out + captured.err).lower()
