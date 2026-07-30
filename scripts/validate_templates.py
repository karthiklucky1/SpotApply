"""Validate every Jinja template: template parses, and each inline <script>
block is syntactically valid JavaScript.

Why this exists: app/templates/dashboard.html is one ~7k-line template with
large inline <script> blocks. A stray brace or a broken Jinja tag there is
invisible to pytest (no test renders the whole dashboard) but breaks the entire
app in the browser. This is the guard that catches it — run locally before
committing a template edit, and in CI on every push.

Usage:
    python scripts/validate_templates.py                # all templates
    python scripts/validate_templates.py app/templates/dashboard.html

Requires: jinja2 (always available), node (optional — JS checking is skipped
with a warning when node is absent, so the Jinja check still runs).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import jinja2
except ImportError:  # pragma: no cover - CI installs it
    print("jinja2 not installed — cannot validate templates", file=sys.stderr)
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "app" / "templates"

# Jinja constructs are stripped before handing a script block to node: the
# server-side values they emit are not JS syntax at check time.
_EXPR = re.compile(r"\{\{.*?\}\}", re.DOTALL)      # {{ value }}
_STMT = re.compile(r"\{%-?.*?-?%\}", re.DOTALL)    # {% if %} / {% for %}
_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)     # {# note #}
_SCRIPT = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.DOTALL)


def _strip_jinja(js: str) -> str:
    """Replace Jinja expressions with a literal so the JS stays parseable."""
    js = _COMMENT.sub("", js)
    js = _EXPR.sub("0", js)
    js = _STMT.sub("", js)
    return js


def check_template(path: Path, node: str | None) -> list[str]:
    """Return a list of human-readable problems (empty == valid)."""
    problems: list[str] = []
    src = path.read_text(encoding="utf-8")

    # 1. Jinja parse — catches unbalanced {% %}, bad filters, typos.
    try:
        jinja2.Environment().parse(src)
    except Exception as e:  # jinja2.TemplateSyntaxError and friends
        line = getattr(e, "lineno", "?")
        problems.append(f"Jinja parse error at line {line}: {e}")
        return problems  # a template that will not parse cannot be JS-checked

    if node is None:
        return problems

    # 2. Each inline script block must be valid JS.
    for i, m in enumerate(_SCRIPT.finditer(src)):
        attrs, body = m.group("attrs"), m.group("body")
        if "src=" in attrs:            # external file, nothing inline to check
            continue
        if "type=" in attrs and "javascript" not in attrs and "module" not in attrs:
            continue                    # e.g. application/ld+json, x-template
        if not body.strip():
            continue
        line_no = src[: m.start()].count("\n") + 1
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(_strip_jinja(body))
            tmp = fh.name
        try:
            r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
            if r.returncode != 0:
                detail = (r.stderr or r.stdout).strip().splitlines()
                msg = detail[-1] if detail else "unknown syntax error"
                problems.append(
                    f"JS syntax error in <script> block #{i + 1} "
                    f"(starts near line {line_no}): {msg}"
                )
        finally:
            Path(tmp).unlink(missing_ok=True)
    return problems


def main(argv: list[str]) -> int:
    node = shutil.which("node")
    if node is None:
        print("! node not found — checking Jinja only, skipping JS syntax", file=sys.stderr)

    targets = [Path(a) for a in argv[1:]] or sorted(TEMPLATE_DIR.rglob("*.html"))
    if not targets:
        print(f"No templates found under {TEMPLATE_DIR}", file=sys.stderr)
        return 2

    failed = 0
    for path in targets:
        if not path.exists():
            print(f"FAIL {path}: file not found")
            failed += 1
            continue
        problems = check_template(path, node)
        rel = path.relative_to(ROOT) if ROOT in path.parents else path
        if problems:
            failed += 1
            print(f"FAIL {rel}")
            for p in problems:
                print(f"     - {p}")
        else:
            print(f"ok   {rel}")

    print(f"\n{len(targets) - failed}/{len(targets)} templates valid")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
