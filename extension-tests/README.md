# Extension live tests

Loads the real MV3 extension from `../extension` into Chromium via Playwright
and drives the production flow end to end against a throwaway local ATS —
no staging backend, no manual clicking.

Kept OUT of `extension/` on purpose: `GET /api/extension/download` zips that
folder recursively, so anything inside it ships to every user.

```bash
pip install playwright && playwright install chromium   # once
python3 extension-tests/test_extension_live.py          # 13 checks — plumbing
python3 extension-tests/test_extension_forms.py         # 29 checks — real fills
python3 extension-tests/test_extension_security.py      #  7 checks — gates
```

Set `SPOTAPPLY_CHROMIUM=/path/to/chrome` to use a specific binary (the sandbox's
pre-installed `/opt/pw-browsers/chromium` is picked up automatically).

**test_extension_live.py** — the happy path: service worker registers, popup
renders its empty state with a clean console, the dashboard `PING` → `PING_OK`
liveness bridge answers **in both protocol dialects** (`SPOTAPPLY_*` is what the
dashboard sends now; `HIREPATH_*` must keep working for a cached older page),
`LOAD_PACK` is ACKed, the background worker opens the apply tab, and
`tabs.onUpdated` auto-fires `DO_FILL` so the fields land. Its pack deliberately
carries the legacy `hirepath_url` key to cover the back-compat read path.

**test_extension_forms.py** — proves the FILL ITSELF against four DOM shapes,
with a stub backend serving the résumé so the attach path runs for real:

| Layout | What it pins down |
| --- | --- |
| Greenhouse | labels/ids/names, EEO selects, textarea, résumé upload, sponsorship + work-auth Yes/No |
| Lever | single "Full name" field, bare `name=` attributes |
| Workday | `data-automation-id` only, no usable labels, country dropdown |
| React | controlled inputs that revert any value written without a native setter |

Two real bugs came out of this file that static review missed: `/unit/` matching
"**Unit**ed States" (so *"Are you legally authorized to work in the United
States?"* was silently classified as an address-line-2 field and skipped), and
the work-auth status string being fed to Yes/No sponsorship dropdowns.

**test_extension_security.py** — the gates that keep a live copilot session from
acting on unrelated sites. It records every HTTP request the extension makes, so
a regression shows up as a real request, not an inference:

| Check | Guards against |
| --- | --- |
| S1 | autofilling PII into any page with a lone email input (a newsletter box) |
| S2 | POSTing dropdown choices from other sites to `/api/save-answer` |
| S3 | a stray form submit marking the application SUBMITTED |
| S4 | that same submit destroying the in-progress fill session |
| S5–S6 | regression: real ATS fill and real submit-tracking still work |

S5/S6 are what stop the gates from being tightened into uselessness — run the
whole file after touching `hpCopilotSurface()` or anything in the fill path.

To watch it happen, flip `headless=True` to `False`.
