"""An HTML entity inside a Jinja string expression is escaped twice.

Autoescape escapes the VALUE of ``{{ ... }}``, so ``{{ 'Auto-Fill &amp; Apply' }}``
reaches the page as ``&amp;amp;`` and every board card read "Auto-Fill &amp;amp;
Apply" (shipped with one-click submit, 2026-10-11). Inside an expression write
the plain character (``'Auto-Fill & Apply'``) and let autoescape encode it;
entities belong only in literal template text.
"""
import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"
EXPR = re.compile(r"\{\{(.*?)\}\}", re.S)
STRING = re.compile(r"'([^'\\]*)'|\"([^\"\\]*)\"")
ENTITY = re.compile(r"&(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);")


def test_no_html_entity_inside_a_jinja_string_expression():
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for m in EXPR.finditer(text):
            body = m.group(1)
            if "|safe" in body.replace(" ", ""):
                continue  # explicitly marked safe: not escaped again
            for s in STRING.finditer(body):
                literal = s.group(1) if s.group(1) is not None else s.group(2)
                if ENTITY.search(literal):
                    line = text.count("\n", 0, m.start()) + 1
                    offenders.append(f"{path.name}:{line}: {literal!r}")
    assert not offenders, "entity inside {{ }} is double-escaped:\n" + "\n".join(offenders)
