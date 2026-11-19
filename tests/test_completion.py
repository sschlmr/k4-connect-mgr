"""The zsh completion script k4cm installs completes on the first Tab."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from k4cm.client.main import app


@pytest.fixture(autouse=True)
def as_zsh(monkeypatch):
    """Typer asks shellingham which shell is running; under pytest, say zsh."""
    monkeypatch.setattr("typer.completion._get_shell_name", lambda: "zsh")
    monkeypatch.setattr("typer._completion_shared._get_shell_name", lambda: "zsh")


def test_typer_still_takes_our_zsh_script():
    from k4cm.client.completion import patch_typer_zsh

    assert patch_typer_zsh(), "Typer's completion internals moved: revisit k4cm/client/completion.py"


def _zsh_script() -> str:
    result = CliRunner().invoke(app, ["--show-completion"], prog_name="k4cm")
    assert result.exit_code == 0, result.output
    return result.output


def test_show_completion_gives_the_corrected_script():
    script = _zsh_script()
    assert script.startswith("#compdef k4cm")
    assert "loadautofunc" in script and '_k4cm_completion "$@"' in script
    assert "compdef _k4cm_completion k4cm" in script


def test_install_completion_writes_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    result = CliRunner().invoke(app, ["--install-completion"], prog_name="k4cm")
    assert result.exit_code == 0, result.output
    assert "loadautofunc" in (tmp_path / ".zfunc" / "_k4cm").read_text()


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")
def test_script_is_valid_zsh(tmp_path):
    path = tmp_path / "_k4cm"
    path.write_text(_zsh_script())
    subprocess.run(["zsh", "-n", str(path)], check=True)
