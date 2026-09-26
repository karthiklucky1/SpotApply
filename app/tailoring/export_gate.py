"""ONE answer to "may this tailored document leave the product?"

AUDIT 2026-09-25 (finding 5). The pre-download review detected a skill the
résumé never evidences written into the draft as done work (`unconfirmed_claims:
["Kafka"]`) and in the same response reported `download_blocked: false`,
because the flag looked only at the application's status. The review warned;
the download, the preview, the extension's fill-pack and its résumé attach all
served the document anyway. Four routes each carried their own copy of
"status == ERROR", and none of them knew what the review knew.

So there is one verdict, computed here, and every door that hands the draft
out — review, details/preview, DOCX download, fill-pack text, fill-pack
attach — asks for it:

    grounding rejected the draft (status ERROR)        → blocked
    the draft claims a skill the master résumé never
    evidences (`requirements.review().unconfirmed_claims`) → blocked
    otherwise                                           → allowed

It is BOUND to its inputs: the verdict is cached under a hash of the master
résumé, the job description and the draft text, so an edited résumé, a
re-tailored draft or a changed posting is a different key and is judged afresh
— nothing has to remember to invalidate it. Deterministic, no LLM, no network.

A genuine gap (the posting wants five years; the résumé shows two) never blocks
anything: that is a fact about the candidate, explained in the review. Only a
claim the DOCUMENT makes that the résumé cannot back does.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

#: Bumped when the rules below change, so a cached verdict from the old rules
#: is never served (it is part of the cache key).
GATE_VERSION = 1

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_S = 1800.0
_CACHE_MAX = 512


@dataclass(frozen=True)
class ExportVerdict:
    allowed: bool
    code: str                       # ok | grounding_rejected | unconfirmed_claims | no_draft
    reason: str                     # the sentence a user reads when blocked
    unconfirmed_claims: Tuple[str, ...] = ()
    basis: str = ""                 # hash of the inputs this verdict is bound to
    review: Optional[object] = field(default=None, compare=False, repr=False)

    @property
    def blocked(self) -> bool:
        return not self.allowed


def _basis(master: str, tailored: str, jd: str) -> str:
    h = hashlib.sha256()
    for part in (str(GATE_VERSION), master or "", tailored or "", jd or ""):
        h.update(part.encode("utf-8", "ignore"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def evaluate(*, grounding_rejected: bool, grounding_reason: str,
             master: str, tailored: str, jd: str) -> ExportVerdict:
    """The verdict for one draft. Pure apart from the content-keyed cache."""
    if grounding_rejected:
        return ExportVerdict(
            False, "grounding_rejected",
            (grounding_reason or "").strip()
            or "This résumé did not pass the grounding check and cannot be exported. "
               "Re-run tailoring for this application.")
    if not (tailored or "").strip():
        # Nothing to judge — the caller has no draft to hand out either.
        return ExportVerdict(True, "no_draft", "")
    if not (master or "").strip():
        # Without the master résumé there is nothing to check claims AGAINST.
        # Refusing here would lock every user whose résumé lives only in a
        # profile out of their own documents; grounding already checked the
        # draft against the same inputs at generation time.
        return ExportVerdict(True, "ok", "")

    key = _basis(master, tailored, jd)
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]

    from app.tailoring.requirements import review as build_review
    rep = build_review(master, tailored, jd)
    claims = tuple(rep.unconfirmed_claims)
    if claims:
        verdict = ExportVerdict(
            False, "unconfirmed_claims",
            ("This draft states experience your résumé does not show: "
             + ", ".join(claims[:5])
             + ". Remove it (or add where you really did it to your master résumé) "
               "and re-run tailoring before exporting."),
            unconfirmed_claims=claims, basis=key, review=rep)
    else:
        verdict = ExportVerdict(True, "ok", "", basis=key, review=rep)
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[: _CACHE_MAX // 4]:
                _CACHE.pop(k, None)
        _CACHE[key] = (now, verdict)
    return verdict


def reset_state() -> None:
    """Tests only."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ── removing an unsupported claim instead of withholding the document ────────

_ITEM_SPLIT = re.compile(r"\s*(?:,|\||·|;|•)\s*")


def strip_unconfirmed(md: str, claims) -> "tuple[str, list[str]]":
    """Remove every mention of a skill the master résumé does not evidence.

    Withholding the whole document for one word ("Rust" in a skills line) left
    users with no résumé at all (2026-09-26). The honest fix is narrower: the
    claim comes OUT, everything the résumé does support stays, and the skill is
    reported back as something to learn — or to confirm, if the person really
    has used it. Never adds anything; only removes.

      * a skills/list line loses just that item;
      * in a sentence, "Python and Rust" / "Python, Rust" loses the coordinated
        item; if the claim is still there, the bullet (or sentence) is dropped.
    Returns (new_markdown, claims_actually_removed).
    """
    from app.tailoring.inventory import _skill_pattern
    from app.tailoring.requirements import _number_forms

    claims = [c for c in (claims or []) if (c or "").strip()]
    if not claims or not (md or "").strip():
        return md, []
    pats = {c: [_skill_pattern(f) for f in _number_forms(c)] for c in claims}

    def hits_in(text: str):
        return [c for c, ps in pats.items() if any(p.search(text) for p in ps)]

    removed: list = []
    out: list = []
    section = ""
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("#"):
            section = s.lstrip("#").strip().lower()
            out.append(line)
            continue
        hits = hits_in(line)
        if not hits:
            out.append(line)
            continue
        for c in hits:
            if c not in removed:
                removed.append(c)
        bullet = re.match(r"^(\s*[-*]\s+)", line)
        lead = bullet.group(1) if bullet else ""
        body = line[len(lead):]
        seps = sum(body.count(x) for x in (",", "|", "·", ";", "•"))
        is_list = ("skill" in section or "technolog" in section or "tool" in section
                   or (seps >= 2 and not re.search(r"[.!?]\s*$", body.strip())))
        if is_list:
            label = re.match(r"^((?:\*\*[^*]+\*\*|[^:,|]{1,40}):\s*)", body)
            head = label.group(1) if label else ""
            items = [i for i in _ITEM_SPLIT.split(body[len(head):]) if i.strip()]
            kept = [i for i in items if not hits_in(i)]
            if kept:
                joiner = " | " if "|" in body else " · " if "·" in body else ", "
                out.append(lead + head + joiner.join(kept))
            continue
        # A sentence: drop the coordinated item first ("A, B and Rust").
        new = body
        for c in hits:
            for p in pats[c]:
                src = p.pattern
                # a trailing modifier: ", with Rust experience" / "using Rust"
                new = re.sub(rf",?\s+(?:with|using|in|including|and)\s+(?:hands-on\s+)?(?:{src})"
                             rf"(?:\s+(?:experience|skills|expertise|knowledge))?(?=[.;!?]|$)",
                             "", new, flags=re.I)
                new = re.sub(rf"(?:\s*,\s*|\s+(?:and|or)\s+|\s*/\s*)(?:{src})", "", new, flags=re.I)
                new = re.sub(rf"(?:{src})(?:\s*,\s*|\s+(?:and|or)\s+|\s*/\s*)", "", new, flags=re.I)
        if not hits_in(new):
            out.append(lead + new)
            continue
        if bullet:
            continue                              # the bullet was about the claim
        sentences = re.split(r"(?<=[.!?])\s+", new)
        kept = [x for x in sentences if not hits_in(x)]
        if kept:
            out.append(" ".join(kept))
    return "\n".join(out), removed
