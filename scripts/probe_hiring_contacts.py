#!/usr/bin/env python
"""Live probe: what job-specific people/org evidence do PUBLIC sources expose?

Companion to docs/research/hiring-contacts-2026-09.md. Run it from a machine
with normal egress (the research sandbox could not reach any job host):

    python scripts/probe_hiring_contacts.py \
        --board greenhouse:cloudflare --board greenhouse:crunchyroll \
        --board ashby:zapier --board ashby:posthog --board ashby:ashby \
        --board lever:coupa --board lever:jito.wtf \
        --board smartrecruiters:Wix2 \
        --board workday:https://livenation.wd503.myworkdayjobs.com/TMExternalSite \
        --board recruitee:<slug> --board pinpoint:<slug> --board join:<slug> \
        --hn 49522897 --limit 5 --out probe_results.json

    # individual pages (JSON-LD applicationContact / hiringOrganization):
    python scripts/probe_hiring_contacts.py --url https://jobs.lever.co/coupa/<id> ...

    # USAJOBS needs a free key: export USAJOBS_API_KEY=... USAJOBS_EMAIL=...
    python scripts/probe_hiring_contacts.py --usajobs "software engineer" --limit 5

What it records per job (public fields only, PII redacted by default):
  * every top-level key the endpoint returned            → "missing fields" audit
  * person-like keys anywhere in the payload (creator, recruiter, contact, …)
  * org/req join keys (department, team, refNumber, jobReqId, metadata)
  * the regex assertions from app.intelligence.hiring_contacts over the text
and a per-family summary. Nothing is written to the SpotApply DB. It reads only
unauthenticated public endpoints (plus USAJOBS' free key if you set it) and
never touches LinkedIn/Indeed.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.intelligence.hiring_contacts import (  # noqa: E402
    extract_from_structured, extract_from_text, scan_org_keys, scan_person_like_keys,
)

UA = "SpotApply-research-probe/1.0 (+https://app.spotapply.ai; public job fields only)"
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE = re.compile(r"\+?\d[\d ()-]{7,}\d")


def _strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", html.unescape(s)).strip()


def _redact(v: Any, keep_pii: bool) -> Any:
    if keep_pii:
        return v
    if isinstance(v, str):
        v = _EMAIL.sub("<email>", v)
        return _PHONE.sub("<phone>", v)
    if isinstance(v, dict):
        return {k: _redact(x, keep_pii) for k, x in v.items()}
    if isinstance(v, list):
        return [_redact(x, keep_pii) for x in v]
    return v


def _get(client: httpx.Client, url: str, **kw) -> httpx.Response | None:
    for attempt in range(3):
        try:
            r = client.get(url, **kw)
            if r.status_code in (429, 503):
                time.sleep(2 ** attempt)
                continue
            return r
        except httpx.HTTPError as e:  # network, timeout
            print(f"  ! {url}: {e}", file=sys.stderr)
            time.sleep(2 ** attempt)
    return None


# ── per-family fetchers: return (job_url, text, payload) tuples ───────────────

def fetch_greenhouse(c, slug, limit):
    r = _get(c, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if not r or r.status_code != 200:
        return [], f"http {r.status_code if r else 'none'}"
    jobs = r.json().get("jobs", [])[:limit]
    return [(j.get("absolute_url"), _strip_html(j.get("content", "")), j) for j in jobs], None


def fetch_ashby(c, slug, limit):
    r = _get(c, f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    if not r or r.status_code != 200:
        return [], f"http {r.status_code if r else 'none'}"
    jobs = r.json().get("jobs", [])[:limit]
    return [(j.get("jobUrl"), j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml", "")), j)
            for j in jobs], None


def fetch_lever(c, slug, limit):
    r = _get(c, f"https://api.lever.co/v0/postings/{slug}?mode=json&limit={limit}")
    if not r or r.status_code != 200:
        return [], f"http {r.status_code if r else 'none'}"
    jobs = r.json()[:limit]
    out = []
    for j in jobs:
        text = j.get("descriptionPlain", "") + "\n" + "\n".join(
            _strip_html(x.get("content", "")) for x in j.get("lists", []))
        out.append((j.get("hostedUrl"), text, j))
    return out, None


def fetch_smartrecruiters(c, slug, limit):
    r = _get(c, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit={limit}")
    if not r or r.status_code != 200:
        return [], f"http {r.status_code if r else 'none'}"
    out = []
    for p in r.json().get("content", [])[:limit]:
        d = _get(c, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{p['id']}")
        detail = d.json() if d and d.status_code == 200 else p
        secs = (detail.get("jobAd") or {}).get("sections") or {}
        text = "\n".join(_strip_html((secs.get(k) or {}).get("text", "")) for k in
                         ("companyDescription", "jobDescription", "qualifications", "additionalInformation"))
        url = detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{slug}/{p['id']}"
        out.append((url, text, detail))
    return out, None


def fetch_workday(c, site_url, limit):
    m = re.match(r"https://([^/]+)/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", site_url)
    if not m:
        return [], "bad workday url (expect https://{tenant}.wdN.myworkdayjobs.com/{site})"
    host, site = m.group(1), m.group(2)
    tenant = host.split(".")[0]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    try:
        r = c.post(f"{base}/jobs", json={"appliedFacets": {}, "limit": limit, "offset": 0, "searchText": ""},
                   headers={"Accept": "application/json", "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        return [], str(e)
    if r.status_code != 200:
        return [], f"http {r.status_code}"
    out = []
    for j in r.json().get("jobPostings", [])[:limit]:
        d = _get(c, f"{base}{j.get('externalPath')}", headers={"Accept": "application/json"})
        info = (d.json().get("jobPostingInfo") if d and d.status_code == 200 else None) or {}
        out.append((f"https://{host}/{site}{j.get('externalPath')}", _strip_html(info.get("jobDescription", "")),
                    {"listing": j, "jobPostingInfo": info}))
    return out, None


def fetch_recruitee(c, slug, limit):
    r = _get(c, f"https://{slug}.recruitee.com/api/offers/")
    if not r or r.status_code != 200:
        return [], f"http {r.status_code if r else 'none'}"
    offers = r.json().get("offers", [])[:limit]
    return [(o.get("careers_url"), _strip_html(o.get("description", "") + " " + o.get("requirements", "")), o)
            for o in offers], None


def fetch_pinpoint(c, slug, limit):
    r = _get(c, f"https://{slug}.pinpointhq.com/postings.json")
    if not r or r.status_code != 200:
        return [], f"http {r.status_code if r else 'none'}"
    out = []
    for p in r.json().get("data", [])[:limit]:
        a = p.get("attributes", {})
        out.append((a.get("url"), _strip_html(a.get("description", "")), p))
    return out, None


def fetch_join(c, slug, limit):
    page = _get(c, f"https://join.com/companies/{slug}")
    if not page or page.status_code != 200:
        return [], f"company page http {page.status_code if page else 'none'}"
    m = re.search(r'"id":\s*(\d+),\s*"domain"', page.text) or re.search(r'companies/(\d+)/jobs', page.text)
    if not m:
        return [], "could not find company id in page"
    r = _get(c, f"https://join.com/api/public/companies/{m.group(1)}/jobs")
    if not r or r.status_code != 200:
        return [], f"jobs http {r.status_code if r else 'none'}"
    items = (r.json().get("items") if isinstance(r.json(), dict) else r.json())[:limit]
    return [(j.get("url"), _strip_html(j.get("description", "")), j) for j in items], None


def fetch_hn(c, thread_id, limit):
    r = _get(c, f"https://hn.algolia.com/api/v1/items/{thread_id}")
    if not r or r.status_code != 200:
        return [], f"http {r.status_code if r else 'none'}"
    out = []
    for cm in (r.json().get("children") or [])[:limit]:
        if not cm.get("text"):
            continue
        payload = {"id": cm.get("id"), "author": cm.get("author"), "created_at": cm.get("created_at")}
        out.append((f"https://news.ycombinator.com/item?id={cm.get('id')}", _strip_html(cm["text"]), payload))
    return out, None


def fetch_usajobs(c, keyword, limit):
    key, email = os.environ.get("USAJOBS_API_KEY"), os.environ.get("USAJOBS_EMAIL")
    if not key:
        return [], "NOT TESTED: set USAJOBS_API_KEY (free) and USAJOBS_EMAIL"
    r = _get(c, "https://data.usajobs.gov/api/search",
             params={"Keyword": keyword, "ResultsPerPage": limit},
             headers={"Authorization-Key": key, "User-Agent": email or UA, "Host": "data.usajobs.gov"})
    if not r or r.status_code != 200:
        return [], f"http {r.status_code if r else 'none'}"
    items = (((r.json().get("SearchResult") or {}).get("SearchResultItems")) or [])[:limit]
    out = []
    for it in items:
        d = it.get("MatchedObjectDescriptor", {})
        text = " ".join(str(v) for v in (d.get("UserArea", {}).get("Details", {}) or {}).values() if isinstance(v, str))
        out.append((d.get("PositionURI"), text, it))
    return out, None


def fetch_page_jsonld(c, url):
    r = _get(c, url, headers={"Accept": "text/html"})
    if not r or r.status_code != 200:
        return None, f"http {r.status_code if r else 'none'}"
    blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', r.text, re.S | re.I)
    ld = []
    for b in blocks:
        try:
            ld.append(json.loads(b.strip()))
        except json.JSONDecodeError:
            continue
    return (url, _strip_html(r.text)[:20000], {"json_ld": ld, "has_json_ld": bool(ld)}), None


FETCHERS = {
    "greenhouse": fetch_greenhouse, "ashby": fetch_ashby, "lever": fetch_lever,
    "smartrecruiters": fetch_smartrecruiters, "workday": fetch_workday, "recruitee": fetch_recruitee,
    "pinpoint": fetch_pinpoint, "join": fetch_join,
}


def _record(family: str, ident: str, url: str, text: str, payload: dict, keep_pii: bool) -> dict:
    assertions = extract_from_text(text or "", source_url=url or "")
    assertions += extract_from_structured(family, payload if isinstance(payload, dict) else {}, source_url=url or "")
    return {
        "family": family, "board": ident, "url": url,
        "top_level_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
        "person_like_fields": [(p, _redact(v, keep_pii)) for p, v in scan_person_like_keys(payload)],
        "org_join_fields": [(p, v if not isinstance(v, (dict, list)) else json.dumps(v)[:200])
                            for p, v in scan_org_keys(payload)],
        "assertions": [_redact(a.to_dict(), keep_pii) for a in assertions],
        "named_person_for_this_job": any(a.name and a.evidence_type in ("named_for_this_job", "structured_field", "self_identified")
                                         and a.relationship != "contact_email" for a in assertions),
        "title_only_reporting_line": any(a.relationship == "reporting_manager" and a.evidence_type == "title_only_for_this_job"
                                         for a in assertions),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", action="append", default=[], help="family:slug (see FETCHERS) or workday:<site url>")
    ap.add_argument("--url", action="append", default=[], help="individual job page (JSON-LD check)")
    ap.add_argument("--hn", action="append", default=[], help="HN 'Who is hiring' thread id (Algolia)")
    ap.add_argument("--usajobs", default=None, help="keyword search (needs USAJOBS_API_KEY)")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--keep-pii", action="store_true", help="do NOT redact emails/phones in the output")
    ap.add_argument("--out", default="probe_results.json")
    args = ap.parse_args()

    records: list[dict] = []
    failures: list[dict] = []
    with httpx.Client(timeout=30.0, headers={"User-Agent": UA}, follow_redirects=True) as c:
        for spec in args.board:
            family, _, ident = spec.partition(":")
            fn = FETCHERS.get(family)
            if not fn:
                failures.append({"spec": spec, "error": f"unknown family {family}"})
                continue
            print(f"→ {spec}")
            jobs, err = fn(c, ident, args.limit)
            if err:
                failures.append({"spec": spec, "error": err})
                print(f"  ✗ {err}")
                continue
            for url, text, payload in jobs:
                records.append(_record(family, ident, url, text, payload, args.keep_pii))
        for tid in args.hn:
            print(f"→ hn:{tid}")
            jobs, err = fetch_hn(c, tid, args.limit)
            if err:
                failures.append({"spec": f"hn:{tid}", "error": err})
                continue
            for url, text, payload in jobs:
                records.append(_record("hn", tid, url, text, payload, args.keep_pii))
        if args.usajobs:
            jobs, err = fetch_usajobs(c, args.usajobs, args.limit)
            if err:
                failures.append({"spec": f"usajobs:{args.usajobs}", "error": err})
            for url, text, payload in jobs:
                records.append(_record("usajobs", args.usajobs, url, text, payload, args.keep_pii))
        for url in args.url:
            print(f"→ page {url}")
            res, err = fetch_page_jsonld(c, url)
            if err:
                failures.append({"spec": url, "error": err})
                continue
            u, text, payload = res
            rec = _record("page", url, u, text, payload, args.keep_pii)
            rec["json_ld_application_contact"] = [
                b.get("applicationContact") for b in payload["json_ld"] if isinstance(b, dict) and b.get("applicationContact")]
            records.append(rec)

    # ── summary ──
    by_family: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        f = by_family[r["family"]]
        f["jobs"] += 1
        f["named_person"] += int(r["named_person_for_this_job"])
        f["title_only_reporting"] += int(r["title_only_reporting_line"])
        f["structured_person_fields"] += int(bool(r["person_like_fields"]))
        f["any_assertion"] += int(bool(r["assertions"]))
    summary = {k: dict(v) for k, v in by_family.items()}
    out = {"observed_at": datetime.now(timezone.utc).isoformat(), "argv": sys.argv[1:],
           "summary": summary, "failures": failures, "records": records}
    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=2, default=str)

    print("\nfamily            jobs  named  title-only  struct-person  any")
    for fam, s in summary.items():
        print(f"{fam:<16} {s['jobs']:>5} {s['named_person']:>6} {s['title_only_reporting']:>11} "
              f"{s['structured_person_fields']:>14} {s['any_assertion']:>4}")
    if failures:
        print("\nfailures / NOT TESTED:")
        for f in failures:
            print(f"  {f['spec']}: {f['error']}")
    print(f"\nwrote {args.out} ({len(records)} records)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
