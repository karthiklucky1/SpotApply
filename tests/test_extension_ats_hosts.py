"""The extension's two ATS host lists must agree with each other, and with the
boards discovery actually finds jobs on.

They drifted once: discovery gained recruitee/teamtailor/personio/pinpoint/
breezy/join/rippling scrapers, the extension's lists never followed, so
isKnownATS() was False on those hosts. The copilot then only filled the tab it
opened itself (exact host match) and could not resume across a multi-step form
or a cross-domain hop — a silent, per-board failure with no error anywhere.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = (ROOT / "extension" / "background.js").read_text()
CONTENT = (ROOT / "extension" / "content.js").read_text()
DISCOVERY = ROOT / "app" / "discovery"


def _hosts(js: str) -> set[str]:
    """The trusted ATS domains: the ATS_SUFFIXES array (exact host or real
    subdomain — the old substring regex accepted greenhouse.io.unrelated.example)."""
    m = re.search(r"^const ATS_SUFFIXES = \[(.*?)\];", js, re.M | re.S)
    assert m, "ATS_SUFFIXES not found"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def test_background_and_content_ats_lists_match():
    bg = _hosts(BACKGROUND)
    ct = _hosts(CONTENT)
    assert bg == ct, (
        "extension/background.js and extension/content.js ATS_SUFFIXES "
        f"disagree.\n  only in background: {sorted(bg - ct)}\n  only in content: {sorted(ct - bg)}"
    )


def test_every_discovery_ats_is_recognised_by_the_extension():
    """Each app/discovery/<ats>.py scraper's board domain must be recognised."""
    # module stem -> the apply-URL domain candidates it produces
    expected = {
        "greenhouse": ["greenhouse.io"],
        "lever": ["lever.co"],
        "ashby": ["ashbyhq.com"],
        "workday": ["myworkdayjobs.com", "workday.com"],
        "smartrecruiters": ["smartrecruiters.com"],
        "workable": ["workable.com"],
        "bamboohr": ["bamboohr.com"],
        "recruitee": ["recruitee.com"],
        "teamtailor": ["teamtailor.com"],
        "personio": ["personio"],
        "pinpoint": ["pinpointhq.com"],
        "breezy": ["breezy.hr"],
        "join": ["join.com"],
        "rippling": ["rippling.com"],
    }
    hosts = _hosts(BACKGROUND)
    blob = "|".join(hosts)
    missing = []
    for stem, domains in expected.items():
        if not (DISCOVERY / f"{stem}.py").exists():
            continue  # scraper removed — nothing to recognise
        if not any(d.replace(".", r"\.") in blob or d in blob for d in domains):
            missing.append(f"{stem} ({', '.join(domains)})")
    assert not missing, (
        "discovery finds jobs on boards the extension does not recognise as an "
        f"ATS: {missing}. Add them to ATS_SUFFIXES in extension/background.js AND "
        "extension/content.js."
    )
