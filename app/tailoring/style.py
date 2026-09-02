"""Deterministic style repair — the AI tells we can fix without a model.

The tailor prompt has said "bold at most 2-3 genuinely load-bearing
technologies per section" since it was written. A measured audit found the
model producing 51-52 bold spans anyway, which trips our own fingerprint
detector ("Over-bolded — a human bolds sparingly") and drags the reads-human
score down. Asking the model more firmly is not a fix: an instruction the
generator ignores at temperature 1.0 is not a constraint, it is a hope.

Bolding is one of the few tells that CAN be repaired deterministically, because
removing emphasis changes no words and no facts — the document says exactly the
same thing afterwards. So we cap it in code after generation, and leave the
tells that genuinely require rewriting (bullet-length uniformity, every bullet
opening on an action verb) to the prompt and the rebuild loop.

Three kinds of bold are STRUCTURE and are never touched, because stripping them
would damage parsing rather than presentation:

  * experience/education headers — ``**Title** | Company | Jun 2022 - Mar 2024``.
    evidence.py and doctor.py both read employers, titles and dates off this
    exact shape; unbolding it makes a résumé's own employment history
    unparseable to our integrity checks.
  * skills labels — ``- **Languages**: Python, SQL``. One per line, and the
    label is the list's structure.
  * standalone headers — a line that is nothing but one bold span, e.g. a
    project name.

What is left is inline emphasis inside achievement bullets, which is where the
excess actually lives.
"""
from __future__ import annotations

import re
from typing import List, Tuple

# One **...** span, non-greedy, no newline inside.
_BOLD_RE = re.compile(r"\*\*(?!\s)([^*\n]+?)(?<!\s)\*\*")
_SECTION_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
# "**Senior Backend Engineer** | Acme | ..." — a bold span opening the line and
# followed by a pipe-delimited header.
_HEADER_LINE_RE = re.compile(r"^\s*(?:[-*]\s*)?\*\*[^*\n]+\*\*\s*\|")
# "- **Languages**: Python, SQL" — a bold label introducing a list.
_LABEL_LINE_RE = re.compile(r"^\s*(?:[-*]\s*)?\*\*[^*\n]+\*\*\s*:")
# A line that is nothing but one bold span (a project / sub-section title).
_STANDALONE_BOLD_RE = re.compile(r"^\s*\*\*[^*\n]+\*\*\s*$")

DEFAULT_MAX_BOLD_PER_SECTION = 3


def _is_structural(line: str) -> bool:
    return bool(
        _SECTION_RE.match(line)
        or _HEADER_LINE_RE.match(line)
        or _LABEL_LINE_RE.match(line)
        or _STANDALONE_BOLD_RE.match(line)
    )


def count_bold_spans(md: str) -> int:
    return len(_BOLD_RE.findall(md or ""))


def trim_bold(md: str, max_per_section: int = DEFAULT_MAX_BOLD_PER_SECTION) -> Tuple[str, int]:
    """Cap inline emphasis at `max_per_section` spans per section.

    Returns ``(markdown, removed)``. Spans are kept in document order — the
    first few in a section are the ones a human would plausibly have bolded, and
    keeping the earliest preserves the "lead with what matters" reading. A
    résumé already within the cap comes back byte-identical.
    """
    if not md:
        return md, 0

    out: List[str] = []
    kept_in_section = 0
    removed = 0

    for line in md.splitlines():
        if _SECTION_RE.match(line):
            kept_in_section = 0
            out.append(line)
            continue
        if _is_structural(line) or "**" not in line:
            out.append(line)
            continue

        def _replace(m: re.Match) -> str:
            nonlocal kept_in_section, removed
            if kept_in_section < max_per_section:
                kept_in_section += 1
                return m.group(0)
            removed += 1
            return m.group(1)

        out.append(_BOLD_RE.sub(_replace, line))

    # splitlines() drops a trailing newline; put it back so a résumé already
    # within the cap comes back byte-identical and the "no-op" claim holds.
    result = "\n".join(out)
    if md.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result, removed
