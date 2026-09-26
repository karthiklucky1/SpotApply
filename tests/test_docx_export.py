"""The tailored DOCX is ATS-safe and says exactly what the draft says.

Rendered from a SYNTHETIC candidate through the real `_md_to_docx`: a single
column (no tables, text boxes, headers or footers), headings and bullets as
standard Word styles, markdown emphasis converted rather than leaked as
asterisks, and the candidate's actual residence kept separate from an approved
relocation line.
"""
from __future__ import annotations

from docx import Document

from app.tailoring.tailor import _md_to_docx

MD = """# Alex Rivera
Cincinnati, OH | alex@example.invalid
Open to relocation to Chicago, IL

## Professional Experience
### Software Engineer | Northwind Labs | Jan 2024 - Present
- Built a **FastAPI** service handling 40k requests per day.
- Automated the CI/CD pipeline with GitHub Actions.

## Projects
### Volta (personal project) | Mar 2023 - Jun 2023
- Built a Kubernetes operator in Go.
"""


def test_docx_is_single_column_and_in_order(tmp_path):
    out = tmp_path / "r.docx"
    _md_to_docx(MD, out)
    d = Document(out)
    assert not d.tables
    assert all(not p.text.strip() for s in d.sections for p in s.header.paragraphs)
    text = [p.text for p in d.paragraphs]
    assert text[0] == "Alex Rivera"
    assert text.index("Cincinnati, OH | alex@example.invalid") < text.index("Open to relocation to Chicago, IL")
    assert text.index("Professional Experience") < text.index("Projects")
    joined = "\n".join(text)
    assert "**" not in joined, "markdown emphasis must not leak into the document"
    bullets = [p for p in d.paragraphs if p.style.name == "List Bullet"]
    assert len(bullets) == 3
    assert any(r.bold and r.text == "FastAPI" for r in bullets[0].runs)


def test_a_project_stays_labelled_as_a_project(tmp_path):
    out = tmp_path / "r.docx"
    _md_to_docx(MD, out)
    text = "\n".join(p.text for p in Document(out).paragraphs)
    assert "Volta (personal project)" in text
    assert text.index("Volta") > text.index("Projects")
