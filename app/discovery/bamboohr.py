"""BambooHR public careers widget: https://{slug}.bamboohr.com/jobs/embed2.php

BambooHR's old /careers/list JSON was deprecated in 2024. The current public
source is the embedded widget, enriched per job from the public
/careers/{id}/detail JSON (description + datePosted). Boards are SMB-sized, so
per-job enrichment is bounded and cheap.

THE WIDGET HAS TWO FORMS, and the second one is why this file was rewritten
(2026-11-14). Until October 2026 the widget was static HTML, one
``<li class="BambooHR-ATS-Jobs-Item">`` per job. BambooHR then began serving
the same URL as JSON::

    {"departments": [{"id": 1, "label": "Sales",
                      "positions": [{"id": 33, "name": "...", "url": "...",
                                     "location": "City, Region"}]}],
     "labels": {...}, "success": true, "version": null}

The HTML parser found no ``<li>`` in that, returned ``[]`` with
``fetch_complete = True``, and the lane recorded a successful poll of an empty
board — for all 5,557 active boards, from 2026-10-07 until someone counted
(2,670 of them had been posting). No error was ever logged, because nothing
failed: we read a full answer as silence. So:

  * the body decides the parser (starts with ``{`` → JSON), not a header;
  * a JSON body WITHOUT a ``departments`` list, or an HTML body that is not the
    widget, raises ``BoardUnreadableError`` — never "0 jobs";
  * an empty ``departments`` list is a real answer: no openings.

The detail endpoint moved too: the job now sits under ``result.jobOpening``
(it used to be the top-level object), with ``location.addressCountry`` and
``locationType`` ("0" on-site, "1" remote, "2" hybrid) in place of
``location.country`` / ``isRemote``. Reading the old keys off the new envelope
is why every BambooHR row was stored with no description. Both shapes are read.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from app.discovery.base import (
    EVIDENCE_TEAM_OR_DEPARTMENT,
    BoardUnreadableError,
    GeoEvidence,
    RawJob,
    body_hint,
)
from app.discovery.hiring_context import put
from app.discovery.job_identity import scoped_external_id

log = logging.getLogger(__name__)

_WIDGET = "https://{slug}.bamboohr.com/jobs/embed2.php"
_DETAIL = "https://{slug}.bamboohr.com/careers/{id}/detail"
_MAX_JOBS = 60  # cap per board so enrichment cost stays bounded

# An HTML body with no job rows is an empty widget only when it is the widget.
# The empty widget is a few hundred bytes; a sign-in page, a maintenance page or
# the next redesign is not, and must not be recorded as "no openings".
_WIDGET_MARKER = "bamboohr-ats"
_EMPTY_WIDGET_MAX_BYTES = 600

# `locationType` on the detail record. A structured value the source emits —
# not inferred from a title or a description.
_LOCATION_TYPE = {"0": "onsite", "1": "remote", "2": "hybrid"}

_WS_RE = re.compile(r"\s+")


def _strip_html(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(separator="\n").strip()


def _ws(value) -> str:
    return _WS_RE.sub(" ", value).strip() if isinstance(value, str) else ""


def _abs_url(href: str, slug: str) -> str:
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return f"https:{href}"
    return f"https://{slug}.bamboohr.com{href}"


def _listing_from_json(payload: dict, slug: str) -> List[dict]:
    departments = payload.get("departments")
    if not isinstance(departments, list):
        raise BoardUnreadableError(
            "bamboohr", f"widget JSON has no departments list (keys: {sorted(payload)[:6]})")
    out: List[dict] = []
    for dep in departments:
        if not isinstance(dep, dict):
            continue
        department = _ws(dep.get("label"))
        for p in dep.get("positions") or []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") if p.get("id") is not None else "").strip()
            title = _ws(p.get("name"))
            if not pid or not title:
                continue
            out.append({
                "id": pid, "title": title,
                "url": _abs_url(p.get("url") or f"/careers/{pid}", slug),
                "location": _ws(p.get("location")),
                "department": department,
            })
    return out


def _listing_from_html(text: str, slug: str) -> List[dict]:
    soup = BeautifulSoup(text or "", "html.parser")
    out: List[dict] = []
    for li in soup.select("li.BambooHR-ATS-Jobs-Item"):
        a = li.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        job_url = _abs_url(a["href"], slug)
        # id from bhrPositionID_{id} or the URL tail
        pid = (li.get("id", "") or "").replace("bhrPositionID_", "").strip()
        if not pid:
            pid = job_url.rstrip("/").split("/")[-1]
        if not pid or not title:
            continue
        loc_el = li.select_one(".BambooHR-ATS-Location")
        out.append({
            "id": pid, "title": title, "url": job_url,
            "location": loc_el.get_text(strip=True) if loc_el else "",
            "department": "",
        })
    if not out:
        body = (text or "").strip()
        if len(body) > _EMPTY_WIDGET_MAX_BYTES and _WIDGET_MARKER not in body.lower():
            raise BoardUnreadableError(
                "bamboohr", f"widget is neither JSON nor the HTML widget: {body_hint(body)}")
    return out


def parse_listing(text: str, slug: str) -> List[dict]:
    """Every position the widget lists, in either form it is served in.

    ``[]`` means the board answered and has no openings. A body this cannot
    read raises — see the module docstring for what reading it as empty cost.
    """
    body = (text or "").lstrip()
    if body.startswith("{"):
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            return _listing_from_json(payload, slug)
    return _listing_from_html(text, slug)


def _opening(detail) -> dict:
    """The job record inside a /careers/{id}/detail response, either shape."""
    if not isinstance(detail, dict):
        return {}
    result = detail.get("result")
    if isinstance(result, dict) and isinstance(result.get("jobOpening"), dict):
        return result["jobOpening"]
    return detail


def _posted(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class BambooHRScraper:
    name = "bamboohr"

    def __init__(self, board_slug: str):
        self.board_slug = board_slug

    def fetch(self) -> List[RawJob]:
        widget_url = _WIDGET.format(slug=self.board_slug)
        # Complete only once the whole listing parsed (app/discovery/base.py).
        self.fetch_complete = False
        # "" = the fetch succeeded; an empty list WITH a reason here is not an
        # empty board (app/discovery/base.py `fetch_error`). The two `return []`
        # paths below used to be indistinguishable from "no openings".
        self.last_error = ""
        try:
            r = httpx.get(widget_url, timeout=30.0, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Permanent statuses mean the slug is wrong, gone, or private — let
            # the exception propagate so the discovery pipeline's dead-board
            # recorder retires the registry row (these long-tail ATSes are NOT
            # covered by the Greenhouse/Lever/Ashby validation loop, so a junk
            # slug would otherwise 404 on every cycle forever).
            status = e.response.status_code if e.response is not None else None
            if status in (401, 403, 404, 410):
                raise
            log.warning("BambooHR widget failed for %s: %s", self.board_slug, e)
            self.last_error = f"bamboohr widget returned {status}"
            return []
        except httpx.HTTPError as e:
            log.warning("BambooHR widget failed for %s: %s", self.board_slug, e)
            self.last_error = f"bamboohr widget unreachable: {e}"
            return []

        items = parse_listing(r.text, self.board_slug)
        # No listing-phase `signature_entries` here, on purpose: a detail GET
        # that fails leaves a row with no description, and the parsed-list
        # signature (which carries the detail's location evidence) is what
        # makes the next good poll read as "changed" and write the text.
        jobs: List[RawJob] = []
        with httpx.Client(timeout=20.0, follow_redirects=True) as client:
            for it in items[:_MAX_JOBS]:
                ext_id, title, location = it["id"], it["title"], it["location"]
                department = it["department"]

                # The TITLE is not location evidence: "remote" in a title
                # ("Remote Sensing Engineer") used to make the posting remote.
                description, posted_dt, salary = "", None, None
                country, work_mode, work_mode_field = "", "", ""
                try:
                    d = client.get(_DETAIL.format(slug=self.board_slug, id=ext_id))
                    if d.status_code == 200:
                        opening = _opening(d.json())
                        description = _strip_html(opening.get("description") or "")
                        posted_dt = _posted(opening.get("datePosted"))
                        department = department or _ws(opening.get("departmentLabel"))
                        comp = _ws(opening.get("compensation"))
                        salary = comp[:120] or None
                        loc = opening.get("location") or {}
                        if isinstance(loc, dict):
                            country = _ws(loc.get("addressCountry")) or _ws(loc.get("country"))
                            better = ", ".join(p for p in (
                                _ws(loc.get("city")), _ws(loc.get("state")), country) if p)
                            location = better or location
                        loc_type = opening.get("locationType")
                        if loc_type is not None and str(loc_type).strip() in _LOCATION_TYPE:
                            work_mode = _LOCATION_TYPE[str(loc_type).strip()]
                            work_mode_field = "locationType"
                        elif opening.get("isRemote"):
                            work_mode, work_mode_field = "remote", "isRemote"
                except Exception:
                    pass  # listing row still usable without the detail enrichment
                remote = work_mode == "remote" or "remote" in location.lower()
                geo = GeoEvidence(
                    sites=[location] if location else [],
                    sites_field="location.city+state+addressCountry",
                    country=country, country_field="location.addressCountry" if country else "",
                    work_mode=work_mode, work_mode_field=work_mode_field,
                ) if (location or country or work_mode) else None
                context: dict = {}
                put(context, "department", department, EVIDENCE_TEAM_OR_DEPARTMENT,
                    "departments[].label")

                jobs.append(
                    RawJob(
                        source="bamboohr",
                        # BambooHR numbers jobs per subdomain, so the bare id
                        # collides across employers (job_identity).
                        external_id=scoped_external_id(
                            "bamboohr", self.board_slug, ext_id),
                        company=self.board_slug.replace("-", " ").title(),
                        title=title,
                        location=location,
                        remote=remote,
                        url=it["url"],
                        description=description,
                        posted_at=posted_dt,
                        origin="bamboohr",
                        geo=geo,
                        salary_text=salary,
                        context=context,
                    )
                )
        # items[:_MAX_JOBS] above: a longer board was cut, so it is a subset —
        # a deterministic one, so its poll signature is still a stable baseline.
        self.fetch_complete = len(items) <= _MAX_JOBS
        self.signature_stable = True
        log.info("BambooHR[%s]: %d jobs", self.board_slug, len(jobs))
        return jobs
