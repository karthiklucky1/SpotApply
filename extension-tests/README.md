# Extension live tests

Loads the real MV3 extension from `../extension` into Chromium via Playwright
and drives the production flow end to end against a throwaway local ATS —
no staging backend, no manual clicking.

Kept OUT of `extension/` on purpose: `GET /api/extension/download` zips that
folder recursively, so anything inside it ships to every user.

```bash
pip install playwright && playwright install chromium   # once
python3 extension-tests/test_extension_live.py          # 12 checks
python3 extension-tests/test_extension_security.py      #  7 checks
```

Set `SPOTAPPLY_CHROMIUM=/path/to/chrome` to use a specific binary (the sandbox's
pre-installed `/opt/pw-browsers/chromium` is picked up automatically).

**test_extension_live.py** — the happy path: service worker registers, popup
renders its empty state with a clean console, the dashboard `HIREPATH_EXT_PING`
→ `PING_OK` liveness bridge answers, `HIREPATH_LOAD_PACK` is ACKed, the
background worker opens the apply tab, and `tabs.onUpdated` auto-fires `DO_FILL`
so name/email/phone/LinkedIn land in the right fields.

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
