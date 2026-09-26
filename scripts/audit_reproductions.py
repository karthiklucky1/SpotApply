#!/usr/bin/env python3
"""Reproduce the 2026-09-25 audit findings against whatever checkout is on sys.path[0].
Pure/synthetic only: no network, no production data. Prints JSON."""
import json, sys, os
from datetime import date, timedelta
sys.path.insert(0, os.getcwd())
os.environ.setdefault("SUPABASE_URL", "")
import types, importlib.util
for name, attrs in (('sentence_transformers', {'SentenceTransformer': object, 'CrossEncoder': object}),
                    ('faiss', {}), ('rank_bm25', {'BM25Okapi': object})):
    if importlib.util.find_spec(name) is None:
        m = types.ModuleType(name); m.__path__ = []
        for k, v in attrs.items(): setattr(m, k, v)
        sys.modules[name] = m
u = types.ModuleType('sentence_transformers.util'); u.cos_sim = lambda *a, **k: None
sys.modules.setdefault('sentence_transformers.util', u)
out = {}

def rec(k, reproduced, detail):
    out[k] = {"reproduced": bool(reproduced), "detail": detail}

# F3 relocation targets + missing home
try:
    from app.common.eligibility import decide, GeoPrefs, Geography
    g = Geography(status="resolved", countries=["united states"], sites=["Dallas, TX"], work_mode="onsite")
    try:
        p = GeoPrefs(country="united states", open_to_relocation=True, home_location="Cincinnati, OH", relocation_targets=("chicago, il",))
    except TypeError:
        p = GeoPrefs(country="united states", open_to_relocation=True, home_location="Cincinnati, OH")
    d1 = decide(g, p)
    d2 = decide(g, GeoPrefs(country="united states", home_location=""))
    rec("F3_chicago_only_admits_dallas", d1.status == "eligible", d1.code)
    rec("F3_missing_home_admits_onsite", d2.status == "eligible", d2.code)
except Exception as e:
    rec("F3", None, f"error {e!r}")

# F2 structured remote vs description attendance
try:
    from app.discovery.geo_verify import derive
    from app.discovery.base import RawJob, GeoEvidence
    desc = "This is a hybrid role: you will work 3 days per week in our San Francisco office."
    r = RawJob(source="ashby", external_id="x", company="C", title="SWE", location="US Remote", remote=True,
               url="u", description=desc, geo=GeoEvidence(sites=["United States"], country="US", work_mode="remote"))
    gg = derive(r)
    dd = decide(gg, GeoPrefs(country="united states", home_location="Austin, TX"))
    rec("F2_telecommute_vs_3_days_sf", dd.status == "eligible", f"work_mode={gg.work_mode} decision={dd.status}/{dd.code}")
except Exception as e:
    rec("F2", None, f"error {e!r}")

# F4 skill tenure
try:
    from app.tailoring.inventory import build_inventory
    from app.tailoring.requirements import assess, ExperienceRequirement
    md = ("## Experience\n**Software Engineer** | Acme | Jan 2020 - Dec 2025\n"
          "- Wrote a Python script in Dec 2025 to migrate billing data.\n"
          "- Built Java services handling 40k requests per day.\n")
    inv = build_inventory(md, extra_skills=["Python"])
    a = assess(ExperienceRequirement(verbatim="5 years", months_min=60, skill="Python", required=True), inv)
    rec("F4_one_month_python_is_72", a.status == "supported", f"months={inv.skill('Python').employment_months} status={a.status}")
except Exception as e:
    rec("F4", None, f"error {e!r}")

# F5 review vs export verdict
try:
    from app.tailoring.requirements import review
    master = ("## Professional Experience\n**Engineer** | Northwind | Jan 2024 - Present\n"
              "- Built a FastAPI service handling 40k requests per day.\n## Skills\nPython, FastAPI\n")
    jd = "## Minimum Qualifications\n- 2 years of experience with Kafka.\n"
    draft = master + "- Built a Kafka streaming platform processing 1M events per second.\n"
    rep = review(master, draft, jd)
    try:
        from app.tailoring.export_gate import evaluate
        v = evaluate(grounding_rejected=False, grounding_reason="", master=master, tailored=draft, jd=jd)
        blocked = v.blocked
    except ImportError:
        blocked = False    # the old route computed download_blocked from status only
    rec("F5_unconfirmed_claim_but_downloadable", bool(rep.unconfirmed_claims) and not blocked,
        f"unconfirmed={list(rep.unconfirmed_claims)} export_blocked={blocked}")
except Exception as e:
    rec("F5", None, f"error {e!r}")

# F6 work-auth runway
try:
    from app.intelligence.work_auth import assess_profile
    class P: pass
    p = P(); p.work_authorization = "F-1 STEM OPT"; p.visa_status = ""; p.requires_sponsorship = True
    p.preferred_country = "United States"; p.stem_opt = True
    p.ead_end_date = (date.today() + timedelta(days=7)).isoformat()
    f = assess_profile(p)
    rec("F6_7_days_sold_as_3_years", "3 years" in (f.selling_point + f.headline), f"selling_point={f.selling_point!r}")
except Exception as e:
    rec("F6", None, f"error {e!r}")

# F7 polling counts as activity / grace days
try:
    from app.config import Settings
    grace = Settings.model_fields["dormant_user_grace_days"].default
    import app.api.server as srv
    has_gate = hasattr(srv, "_is_meaningful_request")
    rec("F7_polling_counts_as_activity", not has_gate, f"meaningful_gate={has_gate}")
    rec("F7_grace_21_days", grace >= 21, f"default={grace}")
except Exception as e:
    rec("F7", None, f"error {e!r}")

# F8 public freshness single-flight
try:
    import app.api.server as srv
    rec("F8_public_freshness_no_single_flight", not hasattr(srv, "_PUBLIC_FRESHNESS_LOCK"), "")
except Exception as e:
    rec("F8", None, f"error {e!r}")

# F11 temporary pro default
try:
    from app.config import Settings
    fld = [k for k in Settings.model_fields if "temporary_pro" in k]
    rec("F11_temporary_pro_off_by_default", all(not Settings.model_fields[k].default for k in fld), f"fields={fld}")
except Exception as e:
    rec("F11", None, f"error {e!r}")

# F13 inline favicon
import glob
inl = [os.path.basename(p) for p in glob.glob("app/templates/*.html")
       if any('rel="icon"' in l and "data:image" in l for l in open(p, encoding="utf-8"))]
rec("F13_inline_favicons", bool(inl), inl)
tailwind_cdn = [os.path.basename(p) for p in glob.glob("app/templates/*.html") if "cdn.tailwindcss.com" in open(p, encoding="utf-8").read()]
rec("F13_public_pages_on_tailwind_cdn", bool(tailwind_cdn), tailwind_cdn)
print(json.dumps(out, indent=1))
