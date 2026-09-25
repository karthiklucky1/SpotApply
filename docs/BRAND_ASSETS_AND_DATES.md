# Phase 9 — branding, dates and visible defects

*2026-09-25. Guard: `tests/test_brand_assets.py` (35 tests, live HTTP through the
app). Regenerate the icon with `python -m scripts.build_favicon_ico`.*

## The missing-logo report, and what it actually was

**`/favicon.ico` was serving the SVG file, with `media_type="image/svg+xml"`** —
SVG bytes, an SVG content type, at a `.ico` URL. One route handled both paths:

```python
@app.get("/favicon.svg")
@app.get("/favicon.ico")
def serve_favicon():
    return FileResponse(".../favicon.svg", media_type="image/svg+xml")
```

Modern Chrome will often render that anyway, which is exactly why it survived:
**a developer whose browser already cached the mark from a page visit sees
nothing wrong.** The clients that trust the extension over the content type —
older Chrome, several crawlers, bookmark and tab-restore surfaces — receive an
image they cannot decode and fall back to a blank or generic icon. That is
consistent with a "no logo in Chrome" report that does not reproduce locally.

**Fixed:** `/favicon.ico` now serves a real ICO — three PNG frames (16/32/48) in
an ICO container, which Chrome, Edge, Firefox and Safari all accept — with
`image/x-icon`. `/favicon.svg` keeps serving SVG. Both carry a cache header;
neither requires authentication.

The `.ico` is **generated from `favicon.svg`** by rendering it in the Chromium
Playwright already ships, so the two marks cannot drift into different logos. The
raster is what a browser actually draws, not an approximation of the brand. A
missing `.ico` falls back to the SVG rather than erroring — an icon must never be
able to 500 a page.

### Surfaces checked as distinct things

| surface | before | after |
|---|---|---|
| `<link rel="icon">` in the page head | data-URI SVG, inline | unchanged (works) |
| `/favicon.ico` | **SVG bytes, wrong content type** | real multi-size ICO |
| `/favicon.svg` | ok | ok, now cacheable |
| `manifest.json` install icons | `/static/icon-192.svg`, `/static/icon-512.svg` | verified present and served |
| social card (`og:image`) | landing only | all four public pages |
| `robots.txt`, `sitemap.xml` | ok | verified reachable signed-out |

Also verified by test: every asset resolves **without authentication**, every
manifest icon exists on disk with case-exact paths (the classic bug that works on
a case-insensitive laptop and 404s on Linux), the ICO directory does not lie
about any frame's dimensions, and every page's `og:image` is actually served.

**Not verified, and stated as such:** I cannot open a fresh desktop Chrome
profile or an incognito window here, and I did not test on anyone else's laptop.
What is verified is what the server returns, through the real application, on the
paths a signed-out visitor and a crawler hit. Search-result icons are a separate
matter: Google re-crawls on its own schedule and indexing cannot be forced, so a
correct `/favicon.ico` today does not change a search result today.

## Dates

| file | before | after | why |
|---|---|---|---|
| `privacy.html` | Last updated: August 2026 | **September 2026** | a real revision — see below |
| `terms.html` | Last updated: June 2025 | **unchanged** | the body has not been substantively revised; Phase 9 says preserve genuinely historical dates |
| `landing.html`, `pricing.html` footers | hard-coded `© 2026` | `{{ current_year }}` | correct the day it was typed, wrong every 1 January after |

The privacy date moved because the policy gained something true that it had been
missing, not to look fresh.

### What the policy was missing

The product stores, on `UserProfile`: `work_authorization`, `visa_status`,
`ead_end_date`, `requires_sponsorship`, `stem_opt`, `preferred_country`,
`open_to_relocation`, and (added in Phase 4) `relocation_targets` /
`relocation_timeline`. **The privacy policy named none of it.** Section 1 listed
account, résumé, application, usage and email data; immigration status was not
mentioned anywhere.

Added as its own bullet, describing the implemented behaviour and nothing more:
what is stored, that the user enters it (none of it is inferred from the résumé),
what it is used for (filtering ineligible jobs; preparing answers to the
work-authorisation questions forms ask), that the product asks the user to
confirm where the answer depends on something we do not hold — which is exactly
what Phase 5 built — and that the fields can be cleared at any time.

No new compliance promise was invented. Every sentence describes behaviour that
already exists in the code.

## Other defects found and fixed

* **No social metadata or canonical URL on `/pricing`, `/privacy`, `/terms`.** A
  shared link rendered as a bare URL with no card, and with no canonical the
  trailing-slash and query-string variants of one page can split indexing between
  them. All three now carry `canonical`, `og:*` and `twitter:*`, pointing at the
  existing `og-card.png`.
* **Icons were served with no cache header**, so every tab open re-fetched the
  mark.

## Checked and found clean

* **Internal links**: every `href="/…"` on the four public pages resolves
  (no 4xx, no missing static file).
* **Terms vs Phase 7**: the terms contain no pricing, subscription or refund
  clause, so pulling the paid paths did not contradict them.
* **Static paths**: case-exact against what is on disk.
