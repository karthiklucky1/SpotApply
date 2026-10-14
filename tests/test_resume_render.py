"""The delivered resume: ONE page, PDF + matching Word, no generator traces.

Owner's rules (app/tailoring/render.py): every tailored resume ships as a
one-page PDF and a .docx laid out from the same markdown at the same sizes,
named First_Last_Resume, and neither file's metadata names the software
that made it. When the content does not fit, the layout tightens (never below
9.5pt / 0.5in, always one line to spare for Word) and only then whole bullets
are REMOVED - least relevant to the job first, then the older role - never
rewritten.

All fixtures are synthetic or the committed sample profiles; nothing here
touches the database or the network.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.oxml.ns import qn
from pypdf import PdfReader

from app.tailoring import render
from app.tailoring.render import (
    TIERS,
    document_filename,
    find_generator_markers,
    fit_one_page,
    outside_streams,
    page_count,
    parse_md,
    render_pdf,
    scrub_docx_metadata,
    trim_candidates,
    write_docx,
)

PROFILES = sorted((Path(__file__).resolve().parents[1] / "data" / "profiles").glob("*.md"))

SHORT_MD = """# Alex Rivera
Cincinnati, OH | alex@example.invalid | (513) 555-0100
Backend Engineer

## Summary
- Backend engineer who ships **Python** services.

## Experience
### Software Engineer | Northwind Labs | Jan 2024 - Present
- Built a **FastAPI** service handling 40k requests per day.
- Automated the CI/CD pipeline with GitHub Actions.

## Skills
**Languages:** Python, SQL, Go

## Education
B.S. Computer Science | University of Cincinnati | 2023
"""

# Software/vendor names that must never appear in a delivered file's metadata.
SOFTWARE_MARKERS = ("fpdf", "pyfpdf", "pypdf", "python-docx", "claude", "anthropic",
                    "openai", "ai-generated", "spotapply", "hirepath", "mpdfaa")


def _pdf_text(pdf: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def _norm(s: str) -> str:
    return " ".join(s.split())


# ── parse_md: the _md_to_docx mapping ────────────────────────────────────────

def test_parse_md_mirrors_the_word_mapping():
    md = (
        "# Alex Rivera\n"
        "alex@example.invalid | Columbus, OH\n"
        "Platform Engineer\n"
        "\n"
        "## Experience\n"
        "```\n"
        "### Engineer | Acme | 2020 - 2024\n"
        "- Built **Kafka** pipelines in *Go*.\n"
        "* Star bullet\n"
        "---\n"
        "**Languages:** Python\n"
        "#### Not a heading level we map\n"
    )
    blocks = parse_md(md)
    assert [(b.kind, b.text) for b in blocks] == [
        ("name", "Alex Rivera"),
        ("header", "alex@example.invalid | Columbus, OH"),
        ("header", "Platform Engineer"),
        ("section", "Experience"),
        ("subhead", "Engineer | Acme | 2020 - 2024"),
        ("bullet", "Built Kafka pipelines in Go."),
        ("bullet", "Star bullet"),
        ("para", "Languages: Python"),
        ("para", "#### Not a heading level we map"),
    ]
    bullet = blocks[5]
    assert [(r.text, r.bold, r.italic) for r in bullet.runs] == [
        ("Built ", False, False), ("Kafka", True, False), (" pipelines in ", False, False),
        ("Go", False, True), (".", False, False)]
    # line_no points at the source line, so a trim removes exactly that line
    assert md.splitlines()[bullet.line_no] == "- Built **Kafka** pipelines in *Go*."


def test_parse_md_agrees_with_md_to_docx_paragraph_for_paragraph(tmp_path):
    from app.tailoring.tailor import _md_to_docx

    out = tmp_path / "legacy.docx"
    _md_to_docx(SHORT_MD, out)
    legacy = [(p.style.name, p.text) for p in Document(out).paragraphs]
    style = {"name": "Heading 1", "section": "Heading 2", "subhead": "Heading 3",
             "bullet": "List Bullet"}
    ours = [(style.get(b.kind, "Normal"), b.text) for b in parse_md(SHORT_MD)]
    assert ours == legacy

    new = tmp_path / "new.docx"
    write_docx(SHORT_MD, new, 0)
    assert [(p.style.name, p.text) for p in Document(new).paragraphs] == legacy


# ── Tiers ────────────────────────────────────────────────────────────────────

def _line_multiple(doc) -> float:
    spacing = doc.styles.element.find(
        qn("w:docDefaults") + "/" + qn("w:pPrDefault") + "/" + qn("w:pPr") + "/" + qn("w:spacing"))
    normal = doc.styles["Normal"].paragraph_format.line_spacing
    if normal is not None:
        return float(normal)
    return int(spacing.get(qn("w:line"))) / 240


def _style_numbers(doc) -> dict:
    s = doc.styles
    sec = doc.sections[0]
    return {
        "body": s["Normal"].font.size.pt,
        "h1": s["Heading 1"].font.size.pt,
        "h2": s["Heading 2"].font.size.pt,
        "h3": s["Heading 3"].font.size.pt,
        "bullet": s["List Bullet"].font.size.pt,
        "margins": {round(m.inches, 4) for m in (sec.top_margin, sec.bottom_margin,
                                                 sec.left_margin, sec.right_margin)},
        "para_after": s["Normal"].paragraph_format.space_after.pt,
        "bullet_after": s["List Bullet"].paragraph_format.space_after.pt,
        "h1_after": s["Heading 1"].paragraph_format.space_after.pt,
        "h2_space": (s["Heading 2"].paragraph_format.space_before.pt,
                     s["Heading 2"].paragraph_format.space_after.pt),
        "h3_space": (s["Heading 3"].paragraph_format.space_before.pt,
                     s["Heading 3"].paragraph_format.space_after.pt),
        "line": round(_line_multiple(doc), 4),
    }


def _tier_numbers(t) -> dict:
    return {
        "body": t.body_pt, "h1": t.h1_pt, "h2": t.h2_pt, "h3": t.h3_pt, "bullet": t.body_pt,
        "margins": {t.margin_in}, "para_after": t.para_after_pt,
        "bullet_after": t.bullet_after_pt, "h1_after": t.h1_after_pt,
        "h2_space": (t.h2_before_pt, t.h2_after_pt),
        "h3_space": (t.h3_before_pt, t.h3_after_pt),
        "line": t.line_spacing,
    }


def test_t0_is_the_current_word_look():
    from app.tailoring.tailor import _set_ats_safe_styles

    doc = Document()
    _set_ats_safe_styles(doc)
    assert _style_numbers(doc) == _tier_numbers(TIERS[0])


@pytest.mark.parametrize("idx", range(len(TIERS)))
def test_word_export_uses_the_tier_numbers(tmp_path, idx):
    out = tmp_path / "r.docx"
    write_docx(SHORT_MD, out, idx)
    doc = Document(out)
    t = TIERS[idx]
    assert _style_numbers(doc) == _tier_numbers(t)
    # Calibri really applies: no theme font (the template's minor font is Cambria)
    styles_xml = zipfile.ZipFile(out).read("word/styles.xml").decode()
    assert 'w:asciiTheme="minorHAnsi"' not in styles_xml


def test_tiers_tighten_and_never_pass_the_readability_floor():
    for a, b in zip(TIERS, TIERS[1:]):
        assert b.body_pt <= a.body_pt and b.margin_in <= a.margin_in
        assert b.line_spacing <= a.line_spacing
    assert TIERS[0].body_pt == 11.0 and TIERS[0].margin_in == 0.75
    for t in TIERS:
        assert t.body_pt >= render.MIN_BODY_PT == 9.5
        assert t.margin_in >= render.MIN_MARGIN_IN == 0.5
        assert t.h3_pt >= t.body_pt and t.h1_pt > t.h2_pt >= t.h3_pt


# ── fit_one_page ─────────────────────────────────────────────────────────────

PROTECTED_SECTIONS = ("SUMMARY", "SKILLS", "EDUCATION")


def _section_lines(md: str, word: str) -> list[str]:
    out, inside = [], False
    for ln in md.splitlines():
        if ln.startswith("## "):
            inside = word.lower() in ln.lower()
        if inside:
            out.append(ln)
    return out


def _header_lines(md: str) -> list[str]:
    out = []
    for ln in md.splitlines():
        if ln.startswith("## "):
            break
        out.append(ln)
    return out


def _only_bullets_removed(before: str, after: str, trimmed: list[str]) -> None:
    removed = [ln for ln in before.splitlines() if ln not in after.splitlines()]
    assert len(removed) == len(trimmed)
    assert all(ln.lstrip().startswith(("- ", "* ")) for ln in removed)
    # what remains is the original, in order, word for word
    it = iter(before.splitlines())
    assert all(any(ln == o for o in it) for ln in after.splitlines())


@pytest.mark.parametrize("path", PROFILES, ids=[p.stem for p in PROFILES])
def test_sample_profiles_fit_on_one_page(path):
    assert PROFILES, "data/profiles/*.md are committed samples"
    md = path.read_text(encoding="utf-8")
    res = fit_one_page(md, jd_text="Backend engineer, Python, Kafka, Kubernetes",
                       author="Alex Rivera", title="Resume")
    assert res.pages == 1 == page_count(res.pdf_bytes)
    assert res.tier is TIERS[res.tier_index]
    _only_bullets_removed(md, res.md, res.trimmed)
    for word in PROTECTED_SECTIONS:
        assert _section_lines(res.md, word) == _section_lines(md, word)
    assert _header_lines(res.md) == _header_lines(md)
    print(f"{path.stem}: {res.tier.name}, {len(res.trimmed)} trimmed")


EXTRA_ROLES = """
**Software Engineer** | Acme Analytics | Jan 2020 - Apr 2022 | Columbus, OH
- Built a Kafka ingestion service in Python processing 40M events per day with exactly-once delivery semantics and replay support.
- Migrated a monolith reporting backend to FastAPI microservices on Kubernetes, cutting p95 latency from 900ms to 210ms.
- Wrote PostgreSQL partitioning and indexing changes that reduced nightly batch runtime from 5 hours to 70 minutes.
- Mentored three junior engineers through code review and weekly pairing sessions on the reliability roadmap.

**Junior Developer** | Brightside Labs | Jun 2018 - Dec 2019 | Dayton, OH
- Maintained internal admin tools used by 40 operations staff across two warehouses and a regional call center.
- Added pytest coverage to a legacy billing module, raising coverage from 12% to 78% and catching four latent defects.
- Automated weekly CSV exports to S3 with Airflow, removing a manual step from the monthly finance close process.
- Organized the office hackathon and judged entries alongside the engineering director and the head of product.

**IT Support Intern** | Riverbend College | May 2017 - Aug 2017 | Dayton, OH
- Reimaged 300 lab workstations before the fall term and documented the imaging checklist for the next intern cohort.
- Answered help-desk tickets for faculty and staff, resolving printer, projector and password issues within one business day.
- Wrote a small Python script that reconciled the asset spreadsheet against the network inventory export each week.

**Teaching Assistant** | Riverbend College | Jan 2016 - May 2017 | Dayton, OH
- Ran weekly lab sections for 60 students in an introductory programming course and graded the weekly assignments.
- Held office hours twice a week and wrote the lab handouts on debugging, version control and unit testing basics.
- Built a small autograder in Python that ran student submissions against hidden test cases and emailed the results.
"""


def _long_resume() -> str:
    base = (PROFILES[0].parent / "backend.md").read_text(encoding="utf-8")
    return base.replace("## TECHNICAL PROJECTS", EXTRA_ROLES + "\n## TECHNICAL PROJECTS")


def _roles(md: str) -> list[list[str]]:
    """Bullet lines per role in experience/project sections (test-side reading)."""
    roles: list[list[str]] = []
    trim = False
    cur = None
    for ln in md.splitlines():
        s = ln.strip()
        if s.startswith("## "):
            trim = bool(re.search(r"experience|project", s, re.I))
            cur = None
        elif trim and (s.startswith("### ") or s.startswith("**")):
            if cur is None or cur:
                cur = []
                roles.append(cur)
        elif trim and s.startswith("- "):
            if cur is None:
                cur = []
                roles.append(cur)
            cur.append(s[2:].replace("*", ""))
    return roles


def test_a_long_resume_is_trimmed_to_one_page_without_losing_structure():
    md = _long_resume()
    assert render.layout_pages(md, len(TIERS) - 1) == 2, "even T3 must overflow for this case"
    jd = "Backend engineer: Python, Kafka, Kubernetes, FastAPI, PostgreSQL, distributed systems"
    res = fit_one_page(md, jd_text=jd, author="Alex Rivera", title="Resume")

    assert res.pages == 1 == page_count(res.pdf_bytes)
    assert res.tier_index == len(TIERS) - 1
    assert res.trimmed, "T3 alone could not fit this resume"
    _only_bullets_removed(md, res.md, res.trimmed)
    for word in PROTECTED_SECTIONS:
        assert _section_lines(res.md, word) == _section_lines(md, word)
    assert _header_lines(res.md) == _header_lines(md)
    for line in md.splitlines():  # every heading and role line survives
        if line.startswith("#") or line.startswith("**"):
            assert line in res.md.splitlines()
    before, after = _roles(md), _roles(res.md)
    assert len(after) == len(before)
    assert all(len(r) >= 1 for r in after), "every role keeps at least one bullet"
    # removal is exactly the published order, a prefix of it
    assert res.trimmed == trim_candidates(md, jd)[: len(res.trimmed)]
    # what goes first shares nothing with the job description
    assert not (render._terms(res.trimmed[0]) & render._terms(jd))
    # nothing invented: the PDF says what the trimmed markdown says
    text = _norm(_pdf_text(res.pdf_bytes))
    for gone in res.trimmed:
        assert _norm(gone)[:60] not in text
    # the Word file is written from the same trimmed text
    print(f"long resume: {res.tier.name}, trimmed {len(res.trimmed)}")


TRIM_MD = """# Pat Doe
pat@example.invalid | Columbus, OH

## Summary
- Summary bullet about Python and Kafka that is long enough to be the longest line here by far.
- Second summary bullet.

## Experience
### Senior Engineer | Newco | 2023 - Present
- Built Kafka pipelines in Python.
- Organized the offsite.
**Engineer** | Oldco | 2019 - 2022
- Wrote Python services on Kubernetes with Kafka.
- Planned holiday parties for the office and coordinated the catering.
- Fixed printers.

## Skills
- Python, Kafka, Kubernetes, and a deliberately long skills bullet line

## Education
- B.S. Computer Science, State University, a long education bullet line
"""


def test_trim_order_is_least_relevant_then_oldest_role_then_longest():
    jd = "Python Kafka Kubernetes engineer"
    assert trim_candidates(TRIM_MD, jd) == [
        # every zero-match bullet before any matching one; among those,
        # Oldco (last in the document = the oldest) first, longer first ...
        "Planned holiday parties for the office and coordinated the catering.",
        "Fixed printers.",
        # ... then Newco's. Each role keeps its JD-matching bullet.
        "Organized the offsite.",
    ]
    # relevance decides before length: with a JD that names printers, the
    # shortest bullet is the one Oldco keeps, and the Kafka one goes
    assert trim_candidates(TRIM_MD, "printers")[:2] == [
        "Planned holiday parties for the office and coordinated the catering.",
        "Wrote Python services on Kubernetes with Kafka.",
    ]
    # no JD: ties everywhere -> longest first; summary/skills/education never
    order = trim_candidates(TRIM_MD, "")
    assert order[0].startswith("Planned holiday parties")
    assert not any(s.startswith(("Summary", "Second", "Python, Kafka", "B.S.")) for s in order)


def test_a_short_resume_is_left_alone_and_fitting_is_idempotent():
    res = fit_one_page(SHORT_MD, author="Alex Rivera", title="Resume")
    assert (res.tier_index, res.trimmed, res.pages) == (0, [], 1)
    assert res.md == SHORT_MD
    again = fit_one_page(res.md, author="Alex Rivera", title="Resume")
    assert (again.tier_index, again.trimmed, again.md) == (0, [], SHORT_MD)

    long_fit = fit_one_page(_long_resume(), jd_text="Python Kafka")
    refit = fit_one_page(long_fit.md, jd_text="Python Kafka")
    assert refit.trimmed == [] and refit.md == long_fit.md and refit.pages == 1


# ── Metadata ─────────────────────────────────────────────────────────────────

def test_pdf_metadata_names_the_candidate_not_the_software():
    pdf = render_pdf(SHORT_MD, 0, author="Alex Rivera", title="Alex Rivera - Resume")
    reader = PdfReader(io.BytesIO(pdf))
    meta = reader.metadata
    assert meta["/Author"] == "Alex Rivera"
    assert meta["/Title"] == "Alex Rivera - Resume"
    assert not meta.get("/Producer") and not meta.get("/Creator")
    assert not meta.get("/Subject") and not meta.get("/Keywords")
    assert reader.xmp_metadata is None
    # Info dict, object dictionaries, trailer: no marker at all, "GPT" included
    assert find_generator_markers(outside_streams(pdf)) == []
    # and the whole file, compressed payloads included, for every marker long
    # enough not to occur by chance in deflate output
    low = pdf.lower()
    for marker in SOFTWARE_MARKERS:
        assert marker.encode() not in low, marker
    # fpdf2's constant subset tag is replaced by a content-derived one
    tags = set(re.findall(rb"/BaseFont /([A-Z]{6})\+", pdf))
    assert tags and b"MPDFAA" not in tags


def test_pdf_metadata_rewrite_removes_a_library_producer():
    from fpdf import FPDF

    pdf = FPDF(unit="pt", format="letter")
    pdf.set_producer("PyFPDF/fpdf2.7.9")
    pdf.set_creator("PyFPDF")
    pdf.add_page()
    pdf.set_font("helvetica", size=11)
    pdf.text(72, 72, "Alex Rivera")
    raw = bytes(pdf.output())
    assert find_generator_markers(raw)

    clean = render._rewrite_pdf_metadata(raw, "Alex Rivera", "Resume")
    meta = PdfReader(io.BytesIO(clean)).metadata
    assert meta["/Author"] == "Alex Rivera" and meta["/Title"] == "Resume"
    assert "/Producer" not in meta and "/Creator" not in meta
    assert find_generator_markers(outside_streams(clean)) == []
    assert page_count(clean) == 1


DOCX_MARKERS = ("python-docx", "generated by", "claude", "anthropic", "openai",
                "spotapply", "hirepath", "microsoft macintosh word", "ai-generated")


def _assert_clean_docx(path: Path, author: str, title: str) -> None:
    z = zipfile.ZipFile(path)
    names = z.namelist()
    assert not any(n.startswith("customXml/") for n in names)
    assert "docProps/thumbnail.jpeg" not in names
    for n in names:
        body = z.read(n).decode("utf-8", "replace").lower()
        for marker in DOCX_MARKERS:
            assert marker not in body, f"{marker!r} in {n}"
        if n.endswith(".rels") or n == "[Content_Types].xml":
            assert "thumbnail" not in body and "customxml" not in body, n
    core = z.read("docProps/core.xml").decode()
    assert f"<dc:creator>{author}</dc:creator>" in core
    assert f"<cp:lastModifiedBy>{author}</cp:lastModifiedBy>" in core
    assert f"<dc:title>{title}</dc:title>" in core
    assert "<cp:revision>1</cp:revision>" in core
    for empty in ("dc:subject", "cp:keywords", "dc:description", "cp:category"):
        assert f"<{empty}>" not in core.replace(f"<{empty}/>", "")
    app = z.read("docProps/app.xml").decode()
    for tag in ("Application", "AppVersion", "Company", "Pages", "Template"):
        assert f"<{tag}>" not in app and f"<{tag}/>" not in app
    props = Document(path).core_properties
    assert props.author == author and props.last_modified_by == author
    assert props.title == title and props.revision == 1 and not props.comments
    assert props.created is not None and props.created.year >= 2026


def test_docx_metadata_is_scrubbed_and_the_document_stays_ats_safe(tmp_path):
    out = write_docx(SHORT_MD, tmp_path / "r.docx", 2, author="Alex Rivera",
                     title="Alex Rivera - Resume")
    _assert_clean_docx(out, "Alex Rivera", "Alex Rivera - Resume")
    d = Document(out)
    assert not d.tables
    assert all(not p.text.strip() for s in d.sections for p in s.header.paragraphs)
    assert all(not p.text.strip() for s in d.sections for p in s.footer.paragraphs)
    assert "**" not in "\n".join(p.text for p in d.paragraphs)
    bullets = [p for p in d.paragraphs if p.style.name == "List Bullet"]
    assert any(r.bold and r.text == "FastAPI" for r in bullets[1].runs)
    xml = zipfile.ZipFile(out).read("word/document.xml").decode()
    assert "<w:txbxContent" not in xml and "<w:drawing" not in xml


def test_scrub_cleans_a_docx_from_the_existing_exporter(tmp_path):
    from app.tailoring.tailor import _md_to_docx

    out = tmp_path / "legacy.docx"
    _md_to_docx(SHORT_MD, out)
    assert b"python-docx" in zipfile.ZipFile(out).read("docProps/core.xml")
    before = [p.text for p in Document(out).paragraphs]
    scrub_docx_metadata(out, author="Alex Rivera", title="Resume")
    _assert_clean_docx(out, "Alex Rivera", "Resume")
    assert [p.text for p in Document(out).paragraphs] == before


# ── Text: non-ASCII, extraction ──────────────────────────────────────────────

UNICODE_MD = """# José Müller-Ångström
Zürich, Switzerland | jose@example.invalid

## Experience
### Ingénieur Logiciel | Société Générale | 2021 – 2024
- Rebuilt the “naïve” pricing service — p95 down 40% → 120ms • 3 regions.
- Led the team’s Kafka rollout (Köln, Malmö, Kraków); ½ the incidents.
- R\u00e9sum\u00e9 parser for \u4e2d\u6587 and emoji \U0001f680 input, zero\u200bwidth and soft\u00adhyphen safe.

## Education
M.Sc. Informatik, ETH Zürich
"""


def test_non_ascii_text_renders_and_extracts(tmp_path):
    pdf = render_pdf(UNICODE_MD, 0, author="José Müller-Ångström", title="Résumé")
    assert page_count(pdf) == 1
    meta = PdfReader(io.BytesIO(pdf)).metadata
    assert meta["/Author"] == "José Müller-Ångström"
    text = _norm(_pdf_text(pdf))
    for s in ("José Müller-Ångström", "Zürich", "Ingénieur Logiciel", "Société Générale",
              "“naïve”", "team’s", "Köln, Malmö, Kraków", "2021 – 2024", "→ 120ms"):
        assert s in text, s
    assert "zerowidth" in text and "softhyphen" in text  # invisible marks stay invisible
    out = write_docx(UNICODE_MD, tmp_path / "u.docx", 0, author="José Müller-Ångström")
    words = "\n".join(p.text for p in Document(out).paragraphs)
    assert "中文" in words and "José Müller-Ångström" in words  # Word keeps every character


def test_pdf_text_is_real_extractable_text():
    md = PROFILES[0].read_text(encoding="utf-8")
    res = fit_one_page(md)
    text = _pdf_text(res.pdf_bytes)
    assert render.resume_name(md) and render.resume_name(md) in text
    norm = _norm(text)
    for b in parse_md(res.md):
        if b.kind == "bullet":
            assert _norm(b.text)[:50] in norm
    reader = PdfReader(io.BytesIO(res.pdf_bytes))
    page = reader.pages[0]
    assert not page.images, "real text, no images"
    assert float(page.mediabox.width) == 612 and float(page.mediabox.height) == 792


def test_pdf_and_docx_carry_the_same_paragraphs(tmp_path):
    md = PROFILES[-1].read_text(encoding="utf-8")
    res = fit_one_page(md)
    out = write_docx(res.md, tmp_path / "r.docx", res.tier_index)
    paras = [p.text for p in Document(out).paragraphs]
    assert paras == [b.text for b in parse_md(res.md)]
    text = _norm(_pdf_text(res.pdf_bytes))
    for p in paras:
        assert _norm(p)[:40] in text


# ── File names ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("args,expected", [
    (("Jane", "Doe", "Acme Inc."), "Jane_Doe_Acme_Resume.docx"),
    (("José", "Müller", "Société Générale"), "Jose_Muller_Societe_Generale_Resume.docx"),
    (("Jane", "Doe", ""), "Jane_Doe_Resume.docx"),
    (("Jane", "Doe", "   "), "Jane_Doe_Resume.docx"),
    (("", "", "Acme"), "Candidate_Acme_Resume.docx"),
    (("", "  ", ""), "Candidate_Resume.docx"),
    (("Jane", "", "Acme"), "Jane_Acme_Resume.docx"),
    (("Jane", "Doe", "Acme Co., Ltd."), "Jane_Doe_Acme_Resume.docx"),
    (("Jane", "Doe", "Siemens AG"), "Jane_Doe_Siemens_Resume.docx"),
    (("Jane", "Doe", "Stripe, LLC"), "Jane_Doe_Stripe_Resume.docx"),
    (("Jane", "Doe", "Booking Holdings B.V."), "Jane_Doe_Booking_Holdings_Resume.docx"),
    (("Jane", "Doe", "J.P. Morgan Chase & Co."), "Jane_Doe_JP_Morgan_Chase_Resume.docx"),
    (("Seán", "O'Brien", "Procter & Gamble"), "Sean_OBrien_Procter_Gamble_Resume.docx"),
    (("PRIYA", "RAMANATHAN", "The Home Depot"),
     "Priya_Ramanathan_The_Home_Depot_Resume.docx"),
    (("Łukasz", "Weiß", ""), "Lukasz_Weiss_Resume.docx"),
    (("Søren", "Ærø", ""), "Soren_AEro_Resume.docx"),
    (("SEÁN", "O'BRIEN", ""), "Sean_OBrien_Resume.docx"),
    (("Seán", "O’Brien", ""), "Sean_OBrien_Resume.docx"),
    (("Đorđe", "Þórsson", ""), "Dorde_Thorsson_Resume.docx"),
    (("Mary-Jane", "MCDONALD-SMITH", ""), "Mary_Jane_Mcdonald_Smith_Resume.docx"),
    (("ANNA", "WEI\u00df", ""), "Anna_Weiss_Resume.docx"),
    (("J\u00dcRGEN", "STRA\u00dfBURG", ""), "Jurgen_Strassburg_Resume.docx"),
    (("Jane", "Doe", "Inc"), "Jane_Doe_Inc_Resume.docx"),
])
def test_document_filename(args, expected):
    assert document_filename(*args) == expected


def test_document_filename_kinds_extensions_and_caps():
    assert document_filename("Jane", "Doe", "AT&T", kind="Cover_Letter", ext="pdf") \
        == "Jane_Doe_ATT_Cover_Letter.pdf"
    assert document_filename("Jane", "Doe", "Acme", kind="Cover Letter", ext=".PDF") \
        == "Jane_Doe_Acme_Cover_Letter.pdf"
    long_name = document_filename("Jane", "Doe", "International Business Machines of the Greater Ohio Valley Corporation")
    company = long_name[len("Jane_Doe_"):-len("_Resume.docx")]
    assert len(company) <= 40 and not company.endswith("_") and company.startswith("International")
    assert re.fullmatch(r"[A-Za-z0-9_]+\.docx", document_filename("李", "雷", "腾讯 / Tencent!"))


# ── Review 2026-10-08: role shapes, glyphs, Word margin, ids ────────────────

BULLET_ROLES_MD = """# Pat Doe
pat@example.invalid | Columbus, OH

## Professional Experience
- **Senior Engineer** | Newco | 2023 - Present
- Built Kafka pipelines in Python.
- Organized the offsite.
- **Engineer** | Oldco | 2019 - 2022
- Wrote Python services on Kubernetes.
- Planned holiday parties for the office.
Analyst, Midco, Jun 2017 - May 2019
- Fixed printers.
- Ran the weekly status meeting.

## Career History
### Intern | Firstco | 2016
- Answered phones.
- Filed reports.
"""


def test_bulleted_role_headers_are_never_trimmed_and_every_role_keeps_a_bullet():
    order = trim_candidates(BULLET_ROLES_MD, "Python Kafka Kubernetes")
    assert not any(o.startswith(("Senior Engineer", "Engineer |")) for o in order)
    # the plain "Analyst, Midco, ..." line starts a role of its own: Midco's
    # bullets are not pooled with Oldco's, so one of them always stays
    assert sum(o in ("Fixed printers.", "Ran the weekly status meeting.") for o in order) == 1
    # "Career History" is a section fitting may trim
    assert sum(o in ("Answered phones.", "Filed reports.") for o in order) == 1
    blocks = render._roles(parse_md(BULLET_ROLES_MD))
    assert [len(r) for r in blocks] == [2, 2, 2, 2]


@pytest.mark.parametrize("line", [
    "- **Senior Engineer**, Newco, 2022 - 2024, Remote",
    "- Data Analyst, Ohio Regional, 2019 - 2021, Columbus OH",
    "- **Engineer** | Oldco | 2019 - 2022",
    "- Engineer, Acme, Jan 2020 - Present",
])
def test_bulleted_role_header_shapes_are_recognised(line):
    md = f"# Pat Doe\n\n## Experience\n{line}\n- Built a thing.\n- Shipped a thing.\n"
    # the JD favours the real bullets, so a header read as a bullet would be
    # the least relevant line of its run and the first to go
    order = trim_candidates(md, "built shipped thing")
    assert line[2:].replace("**", "") not in order
    assert order == ["Shipped a thing."]


def test_relevance_beats_age():
    md = """# Pat Doe

## Experience
### Engineer | Newco | 2023 - Present
- Planned the team offsite in the mountains.
- Shipped a Python feature.
### Engineer | Oldco | 2019 - 2022
- Wrote Kafka consumers in Python.
- Built Kubernetes operators in Go.
"""
    order = trim_candidates(md, "Python Kafka Kubernetes Go")
    # the newer role's off-topic bullet goes before the older role's on-topic one
    assert order[0] == "Planned the team offsite in the mountains."
    assert order.index("Planned the team offsite in the mountains.") \
        < order.index("Built Kubernetes operators in Go.")


@pytest.mark.parametrize("ch,drawn", [
    ("‐", "-"), ("‑", "-"), ("′", "'"), ("∙", "·"),
    ("‧", "·"), ("∶", ":"),
])
def test_punctuation_the_font_lacks_is_drawn_as_its_ascii_kin(ch, drawn):
    assert render._pdf_text(f"co{ch}founded") == f"co{drawn}founded"
    assert render.undrawable_chars(f"co{ch}founded") == []


def test_undrawable_characters_are_reported_not_hidden():
    md = "# 李雷\nlei@example.invalid\n\n## Experience\n- Built a cache.\n"
    res = fit_one_page(md)
    assert res.undrawable == ["李", "雷"]
    assert fit_one_page(SHORT_MD).undrawable == []
    # an emoji is decoration, not content: dropped, not reported
    assert render.undrawable_chars("Shipped \U0001f680 fast") == []


@pytest.mark.parametrize("text,drawn", [
    # LaTeX resumes' PDF text: $\sim$40\%, \cdot / \ast separators, \langle
    ("cut p95 by ∼40%", "cut p95 by ~40%"),
    ("a@b.c ⋅ (614) 555-0100", "a@b.c · (614) 555-0100"),
    ("Python ∗ Go", "Python * Go"),
    ("⟨API⟩", "<API>"),
    # CJK input-method punctuation: "?" is a REAL result here, not a marker
    ("why ship weekly？", "why ship weekly?"),
    ("【Lead】", "[Lead]"),
    ("₽500k budget", "RUB 500k budget"),
    # a math sign with no stand-in is decoration: dropped, never "?"
    ("A ⊕ B", "A  B"),
])
def test_symbols_never_cost_the_pdf(text, drawn):
    """Review 2026-10-08: the PDF is withheld only for letters, digits or
    currency the font cannot draw; a symbol gets a stand-in or is dropped."""
    assert render._pdf_text(text) == drawn
    assert render.undrawable_chars(text) == []
    md = f"# Pat Doe\npat@example.invalid\n\n## Experience\n- {text}\n"
    assert fit_one_page(md).undrawable == []


def test_a_layout_that_fills_the_page_to_the_last_line_moves_to_a_denser_tier():
    """Word wraps with its own engine: a PDF with less than a line to spare
    can be a two-page .docx, so fitting demands one body line of slack."""
    t0 = TIERS[0]
    base = SHORT_MD.replace("## Skills", "{bullets}\n## Skills")
    for n in range(1, 80):
        md = base.format(bullets="\n".join(f"- Item {i}." for i in range(n)))
        if render.layout_pages(md, 0) > 1:
            pytest.fail("never found a layout with less than one line to spare")
        laid = [render._lay(b, t0) for b in parse_md(md)]
        if len(render._paginate(laid, t0, render.word_reserve_pt(t0))) > 1:
            break
    res = fit_one_page(md)
    assert res.tier_index >= 1 and res.trimmed == [] and res.pages == 1


def test_fonttools_does_not_flood_the_log():
    import logging
    assert logging.getLogger("fontTools").getEffectiveLevel() >= logging.WARNING


def test_word_files_do_not_share_the_templates_revision_ids(tmp_path):
    a = write_docx(SHORT_MD, tmp_path / "a.docx", 0)
    b = write_docx(SHORT_MD, tmp_path / "b.docx", 0)

    def ids(path):
        z = zipfile.ZipFile(path)
        settings = z.read("word/settings.xml").decode()
        doc = z.read("word/document.xml").decode()
        root = re.search(r'<w:rsidRoot w:val="([0-9A-F]{8})"', settings).group(1)
        declared = set(re.findall(r'<w:rsid w:val="([0-9A-F]{8})"', settings))
        used = set(re.findall(r'w:rsid\w*="([0-9A-F]{8})"', doc))
        doc_id = re.search(r'w14:docId w14:val="([0-9A-F]{8})"', settings).group(1)
        return root, declared, used, doc_id

    ra, da, ua, ida = ids(a)
    rb, db, ub, idb = ids(b)
    assert "00B47730" not in da | db and ida != "24062061" != idb
    assert ra in da and ua <= da, "document.xml uses only ids settings.xml declares"
    assert ra != rb and da != db and ida != idb
    assert [p.text for p in Document(a).paragraphs] == [b.text for b in parse_md(SHORT_MD)]

    # no id in ANY part is shared by the two files - styles.xml included,
    # whose Header/Footer styles carry one settings.xml never declares
    def every_id(path):
        z = zipfile.ZipFile(path)
        found = set()
        for n in z.namelist():
            if n.startswith("word/") and n.endswith(".xml"):
                xml = z.read(n).decode()
                found |= set(re.findall(r'w:rsid\w*="([0-9A-F]{8})"', xml))
                found |= set(re.findall(r'<w:rsid(?:Root)? w:val="([0-9A-F]{8})"', xml))
        return found
    shared = every_id(a) & every_id(b)
    assert shared == set(), shared
    assert "00E618BF" not in every_id(a)
