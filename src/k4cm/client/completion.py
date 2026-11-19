"""A corrected zsh completion script for `k4cm --install-completion`.

Typer's zsh script (as of 0.27) defines the completer and only calls
`compdef` at the top level. zsh *autoloads* `~/.zfunc/_k4cm` on the first Tab
of each shell and runs its body as the completion function, so that first Tab
just registers the real completer and completes nothing: it beeps, and
completion looks broken until the second Tab. Click's own zsh script avoids
this by calling the completer directly when loaded as an autoload function.

Typer reads its templates from `_completion_shared._completion_scripts` each
time it builds a script, so replacing the zsh entry fixes both
`--install-completion` and `--show-completion`. That dict is private: if a
future Typer drops it, we leave Typer's script alone rather than fail to start,
and `tests/test_completion.py` says so.
"""

from __future__ import annotations

ZSH_SCRIPT = """
#compdef %(prog_name)s

%(complete_func)s() {
  eval $(env _TYPER_COMPLETE_ARGS="${words[1,$CURRENT]}" %(autocomplete_var)s=complete_zsh %(prog_name)s)
}

# Loaded by the first Tab (autoload): complete now, not only register.
if [[ $zsh_eval_context[-1] == loadautofunc ]]; then
  %(complete_func)s "$@"
else
  compdef %(complete_func)s %(prog_name)s
fi
"""


def patch_typer_zsh() -> bool:
    """Swap in the corrected zsh template; False if Typer's internals moved."""
    try:
        from typer import _completion_shared
    except ImportError:
        return False
    scripts = getattr(_completion_shared, "_completion_scripts", None)
    if not isinstance(scripts, dict) or "zsh" not in scripts:
        return False
    scripts["zsh"] = ZSH_SCRIPT
    return True
