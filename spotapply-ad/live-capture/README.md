# Live captures

Re-shoot the app screens the ad uses: run the REAL SpotApply app from this
checkout locally (SQLite, user "local"), seed the fictional demo account, and
screenshot it with Playwright. Use this whenever the UI changes.

```bash
cd spotapply-ad/live-capture
./setup.sh                      # once: Python 3.11 venv, app requirements minus the ML stack
./restart.sh                    # seed fictional data + start the app on 127.0.0.1:8765
. ./env.sh
./venv/bin/python capture.py    # every screen, phone (430x932 @2.5x) + desktop (1280x800 @2x)
./venv/bin/python capture_v2.py # the hero role as SUBMITTED: receipt + outreach email draft (phone)
./venv/bin/python capture_v3.py # desktop: sponsorship card for a candidate who needs it + the v2 screens
kill "$(cat server.pid)"
./publish.sh                    # copy the screens both videos use into ../public/live{,60}/
```

`capture.py` takes step names to re-shoot only part of it, e.g.
`capture.py match tailor` (`board match tailor submitted outreach settings
landing oneclick`).

Timestamps such as "Posted 2 hours ago" are relative to the seed, so capture
right after `restart.sh`. Then re-check the rectangles if the layout moved
(source pixels): the 30-second ad's `src/scenes/S3Find.tsx`, `S4Tailor.tsx`
and `S6Track.tsx` (phone, 1075 px wide), and the explainer's
`src/w60/W3Find.tsx` … `W7Track.tsx` (desktop, 2560 px wide).

## What keeps it safe

- **Fictional data only** (`seed.py`): Maya Chen, Lumen Data, Orchard Systems
  and the other companies are made up; emails use `example.com`. A pool of
  "Sample Employer NNNN" postings only feeds the counters and never appears in
  a frame.
- **No outbound network from the app** (`_netguard.py` refuses every connect)
  and every key in `env.sh` is blank; all background lanes are off. In the
  browser only the local server and Google Fonts load (`capture.py` `router`).
- **Nothing in the repo is modified.** The run root symlinks the repo's
  `app/` and keeps its own `data/`; everything generated (`venv/`, `runroot/`,
  `shots/`, `logs/`) is gitignored.
- **Values the AI normally writes** (scores, reasoning, fit breakdown, Resume
  Doctor numbers, recruiter's read) are written into the seed by hand. The
  tailored DOCX/PDF, earliness receipt, hiring context, review and outreach
  drafts come from the app's own code.
- The Tailoring Studio is opened by calling the page's own
  `openTailorStudio()` / `_tsRenderResult()` on the real `/details` data,
  because reaching it by clicking needs an AI run.

## Phases (`phase.py`)

`untailored` (board + match report), `tailored` (studio / review),
`submitted` (hero role submitted 1h 48m after posting, receipt stamped by
`app.strategy.earliness.stamp`), `oneclick on|off`, and
`needs_sponsorship | citizen`: the demo candidate is a US citizen, so the
sponsorship card is shot once with an F-1 OPT profile that needs
sponsorship (the explainer says so on screen), then the citizen is restored.

## UI issues found while capturing (fixed on this branch)

- Board: the button read "Auto-Fill &amp;amp; Apply" on every card
  (double-escaped `&amp;`; `tests/test_template_entities.py`).
- Desktop: the "Apply today" divider took one grid cell, so the first card
  started in the right column.
- Phone: the job drawer's buttons overlapped its title; the Tailoring Studio
  header and pills overflowed; People & team values wrapped per fragment;
  the outreach job URL overflowed its card; the menu button sat on the corner
  of every modal; four My Profile select labels overflowed a phone-width
  select.
- Outreach: the GitHub note claimed "active open-source contributions" with
  no evidence; LinkedIn fallback drafts ran past the 300-character limit;
  "As Backend Engineer" (missing article); "Sent today" for a message 24.5 h
  old (`_agoLabel` read timestamps without a timezone as local).
- Match report: the sponsorship card said "Sponsorship not stated" to a US
  citizen instead of "No sponsorship needed".
- Review: a skill the posting calls preferred was listed as a genuine gap.
