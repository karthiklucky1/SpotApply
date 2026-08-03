"""No script may do real work at IMPORT time.

scripts/run_matching.py had no __main__ guard and no argparse: line 6 called
run_matching() unconditionally, so `python -m scripts.run_matching --help`
started a real scoring pass — LLM calls, DB writes — and died at the résumé
load. Anything that imports such a module runs it, including a tab-complete,
a doc tool, or a reviewer reading `--help` to find out what it does.

The rule: module top level defines things; main() does things.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

SCRIPTS = sorted(pathlib.Path("scripts").glob("*.py"))

def _app_imports(tree: ast.Module) -> set[str]:
    """Names this module pulled out of the application package.

    Calling one of these is what makes an import do real work: they open the
    DB, hit the network, or spend money. A module-level constant built from a
    pure helper defined in the same file (compiler_replay's regex table,
    cardrace_demo's CANDIDATES) is precomputation, not work — the rule has to
    tell those apart or it just gets suppressed.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app"):
            names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("app"):
                    names.add(a.asname or a.name.split(".")[0])
    return names


def _import_time_calls(tree: ast.Module) -> list[str]:
    """Application calls reachable from a top-level statement, nested included.

    run_discovery.py hid its pass inside `print(f"...{run_discovery()}...")`,
    so a checker that only looked at the outermost call saw a harmless print.
    """
    app = _app_imports(tree)
    out = []
    for node in tree.body:                      # top level ONLY, not inside defs
        if not isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if isinstance(f, ast.Name) and f.id in app:
                out.append(f.id)
            elif isinstance(f, ast.Attribute):
                root = f
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name) and root.id in app:
                    out.append(f"{root.id}.{f.attr}")
    return out


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_script_does_no_work_at_import_time(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = _import_time_calls(tree)
    assert not calls, (
        f"{path} calls {calls} at module level. Importing it — which `--help`, "
        f"a doc tool, or `from scripts import x` all do — would run it. Move the "
        f"body into main() behind `if __name__ == '__main__':`.")


def test_run_matching_help_does_not_start_a_pass(monkeypatch, capsys):
    """The specific incident, pinned: --help must exit before any spend."""
    import scripts.run_matching as rm

    def boom(*a, **k):
        raise AssertionError("--help started a real matching pass")

    monkeypatch.setattr("app.matching.pipeline.run_matching", boom)
    monkeypatch.setattr("sys.argv", ["run_matching", "--help"])
    with pytest.raises(SystemExit) as e:
        rm.main()
    assert e.value.code == 0
    assert "SPENDS MONEY" in capsys.readouterr().out


def test_run_matching_requires_confirmation(monkeypatch, capsys):
    import scripts.run_matching as rm

    def boom(*a, **k):
        raise AssertionError("ran a pass without confirmation")

    monkeypatch.setattr("app.matching.pipeline.run_matching", boom)
    monkeypatch.setattr("sys.argv", ["run_matching", "--user", "u1"])
    monkeypatch.setattr("builtins.input", lambda *_: "")
    assert rm.main() == 1
    assert "Aborted" in capsys.readouterr().out


def test_run_matching_passes_the_user_through(monkeypatch):
    """--user is what makes the console route work at all: without it the
    résumé load falls back to a file the deployed container does not have."""
    import scripts.run_matching as rm
    seen = {}

    monkeypatch.setattr("app.matching.pipeline.run_matching",
                        lambda user_id=None: seen.setdefault("uid", user_id) or [])
    monkeypatch.setattr("sys.argv", ["run_matching", "--user", "abc-123", "--yes"])
    assert rm.main() == 0
    assert seen["uid"] == "abc-123"
