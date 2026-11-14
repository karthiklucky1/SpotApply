# Extension live tests

Loads the real MV3 extension from `../extension` into Chromium via Playwright
and drives the production flow end to end against a throwaway local ATS —
no staging backend, no manual clicking.

Kept OUT of `extension/` on purpose: `GET /api/extension/download` zips that
folder recursively, so anything inside it ships to every user.

```bash
pip install playwright && playwright install chromium   # once
python3 extension-tests/test_extension_live.py          #  13 checks — plumbing
python3 extension-tests/test_extension_forms.py         # 153 checks — real fills
python3 extension-tests/test_extension_payload.py       #  12 checks — payload + EEO
python3 extension-tests/test_extension_security.py      #  11 checks — gates
python3 extension-tests/test_extension_audit.py         #  45 checks — the 2026-09-30 audit
python3 extension-tests/test_extension_queue.py         #  24 checks — the apply queue
python3 extension-tests/test_extension_boards.py        #  46 checks — real board shapes
```

**test_extension_boards.py** — the shapes found on 22 REAL postings (2026-11-14;
nothing submitted, no account made, no CAPTCHA touched), rebuilt as fixtures:

| Fixture | What the real page did | What is pinned |
| --- | --- | --- |
| `greenhouse-react.html` | every dropdown is a pick-one widget; its visible input only searches the list and throws typed text away; the resume input is replaced by a file name | country, authorization and sponsorship are PICKED; nothing is typed into a Yes/No dropdown; the city is searched then picked; "…this position?" never gets the job title; a complete form with empty OPTIONAL boxes submits; an unanswerable REQUIRED dropdown holds it |
| `lever-radios.html` | the question sits two levels above its "Yes." / "No." radios | authorization and both sponsorship wordings ("commence an immigration case") are answered; "Are you local to Austin, TX?" is not guessed and holds a submit |
| `ashby-upload.html` | the site reads the resume, then empties the upload box and shows the file's name | the resume counts as attached (it read as a required field still missing); removing the file opens the field again |
| the same two forms, with saved answers | the profile's auto-fill data reached text boxes and `<select>`s only; one answer was written into a dropdown's hidden bookkeeping input | a saved answer is PICKED in a pick-one dropdown and CHECKED on a radio question when exactly one option says it; no near match, never a work-authorization question, never the hidden input |
| `teamtailor-job.html` | the page's only email box is the employer's staff sign-in | it is not an application form; "Apply for this job" is pressed and the real form filled |
| `smartrecruiters-job.html` | the apply control says "I'm interested" | pressed; "Apply with LinkedIn" never |
| `join-job.html` | "Jetzt bewerben" builds the form on the page | pressed once, the form filled |

**test_extension_queue.py** — the apply queue's worker (`background.js`, "Apply
queue runner") against a stub that plays the server's half
(`/api/apply-queue/next`, `…/outcome`, `/application/{id}/submit`). A mixed list
runs one at a time in its own window: the form that lets it through is submitted
only after the confirmation page, while a CAPTCHA, an unanswered required field
and a fill-only board are each PARKED with their reason and their tab left open.
Also pinned: the parked-tab limit pauses the run and clearing a tab resumes it;
Stop finishes the job in progress and opens no more; the dashboard hears every
change and is never reloaded mid-run. The "submit while the tab is not on screen"
block needs a browser the driver is not attached to — Playwright keeps every
page it drives visible — so it is skipped here and the rule is pinned both ways
by `tests/test_extension_rules.py`.

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
| Recruitee | tabbed form whose application panel is hidden when auto-fill fires: the "Application" tab is PRESSED open (it ignores a bare click) and the form filled; with no such control the fill and the resume wait until the person reveals it |
| Ashby | essay prompt containing "company"; an autofill-from-résumé parser trap |
| Screening | 6 Yes/No radios, an intl-tel widget that mangles the number, a lone dropzone |
| How did you hear | select, Ashby-style option list, radios + "please specify", combobox, text: the profile's saved answer only, all left for the user when none is saved |
| How did you hear, tied | the box after an "Other" pick only when it names Other, shares the question's container or appeared with the pick; "How did you learn Python?" and the next question stay blank; a listbox button's pick is never learned |

Real bugs this file caught that static review missed:

- `/unit/` matched "**Unit**ed States", so *"Are you legally authorized to work
  in the United States?"* was classified as an address-line-2 field and skipped.
- The work-auth status string was fed to Yes/No sponsorship dropdowns, matching
  no option and leaving both required fields blank.
- No site filler handled a single **"Full name"** field, and the advance
  observer only watched `childList` + total field count — so on a tabbed form
  the name was never filled, before OR after revealing the panel.

**test_extension_payload.py** — the payload-handoff class of bug, found by a
live run across Greenhouse, Ashby and Lever that reported *"13 fields filled"*
over a completely empty form and wrote the literal string `undefined undefined`
into name fields. The dashboard's `INIT_EXTENSION` pack is credentials-only and
was being stored as the copilot's FILL pack, so every profile lookup on it was
`undefined`. Pins the whole chain: an auth pack never becomes a fill pack, an
unfillable pack writes nothing and says why, `undefined` never reaches a field,
a real pack still fills, and demographic questions stay untouched unless the
user opts in.

**Demographic questions are never auto-answered by default.** Gender, race,
veteran and disability are voluntary, legally-protected self-identification;
answering them unattended — even with "decline" — is the applicant's call, not
ours. The extension highlights them instead. The popup has an explicit opt-in.

## Reproducing a real posting

When a live application misbehaves, don't guess from a screenshot: open the
extension popup on that page and hit **🩺 Copy diagnostic report**. It puts the
host, whether the host is a recognised ATS, the session state, and every field
(with the signals the matcher saw and who filled it) on the clipboard —
values are redacted to lengths and shapes, so it is safe to paste anywhere.
Turn the interesting part into a fixture in `www/` and add a block here.

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
