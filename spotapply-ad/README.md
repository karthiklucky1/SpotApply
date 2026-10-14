# SpotApply — 30-second social ad + 60-second explainer

A Remotion (React + TypeScript) project that renders two videos for
SpotApply, **"Your experience. Less busywork."**: a 30-second vertical ad
(below) and a [60-second widescreen explainer](#the-60-second-explainer)
with its own layout.

| | |
|---|---|
| Output | `out/spotapply-social-30s.mp4` |
| Format | 1080×1920, 30 fps, 900 frames, 30.0 s, H.264 High, yuv420p, BT.709 (limited range) |
| Audio | Voiceover, AAC 48 kHz. Measured: -13.7 LUFS integrated, -2.0 dBFS true peak. No music. |
| Captions | `out/spotapply-social-30s.srt`, built from the same timeline as the audio |
| Poster | `out/spotapply-social-30s-poster.png` (frame 278: the live match report) |
| Silent cut | `npm run render:silent` (same picture, no voice) |

This folder is separate from the app. Nothing here is imported by, or
deployed with, the SpotApply service (`/.dockerignore` excludes it).

## Preview and render

```bash
cd spotapply-ad
npm install

npm run dev           # Remotion Studio: scrub the ad and each scene
npm run render        # → out/spotapply-social-30s.mp4 (with voiceover)
npm run render:silent # → out/spotapply-social-30s-silent.mp4
npm run captions      # → out/spotapply-social-30s.srt
npm run poster        # → out/spotapply-social-30s-poster.png
npm run render:60     # → out/spotapply-explainer-60s.mp4
npm run captions:60   # → out/spotapply-explainer-60s.srt
npm run poster:60     # → out/spotapply-explainer-60s-poster.png
npm run lint          # eslint + tsc
```

The exact render command behind `npm run render`:

```bash
npx remotion render SpotApplySocial30 out/spotapply-social-30s.mp4 \
  --codec=h264 --crf=16 --x264-preset=slow --image-format=png --color-space=bt709
```

Remotion downloads its own headless Chrome on the first render. If that
download is blocked, point it at an existing headless Chromium:
`REMOTION_BROWSER_EXECUTABLE=/path/to/headless_shell npm run render`.

## The story: one candidate, one role, one application

**LIVE** = a capture of the running app (current `main`, run locally on the
fictional demo account; see [`live-capture/`](live-capture/README.md)).
**Illustrated** = drawn in code, labelled demo data.

| Time | Scene | On screen | Voiceover |
|---|---|---|---|
| 0–3.2 s | `S1Hook` (illustrated) | Three forms get the same name and email typed in. **"Still typing it all again?"** | "Still typing the same details into every application?" |
| 3.2–5.6 s | `S2Reveal` (illustrated) | The forms fold into one resume, which opens as a profile. **"Meet SpotApply"** | "Meet SpotApply." |
| 5.6–10 s | `S3Find` (**LIVE**) | The board with the new **"⚡ Apply today — early window"** divider and the Lumen Data role on top; its **AI match report** slides up (94, why it matched, fit breakdown). **"Find roles that fit."** | "It finds fresh roles that fit your experience, and shows you why." |
| 10–14.4 s | `S4Tailor` (**LIVE**) | **Tailoring Studio**: the summary with the posting's keywords highlighted, then **"Locked to your resume"** (degree, employer, dates never altered) and the **one-page PDF** / Word downloads. **"Your experience. Clearly presented."** | "Then it tailors your resume, using only what you've actually done." |
| 14.4–20.2 s | `S5Apply` (illustrated) | The employer's form fills and the tailored resume attaches; the person's cursor (**You**) checks each field and clicks Submit. Note: **"Optional one-click Apply & Submit: you approve each one"**. **"Less retyping." → "Review. Then submit."** | "It fills the form in your browser. You check it, and the final click is always yours." |
| 20.2–25.2 s | `S6Track` (**LIVE**) | The Submitted tab with the new **earliness receipt** ("⚡ Applied 1h 48m after posting"), then the new **Outreach kit**: an email draft with **Gmail / Outlook / Mail app** links. **"See how early you applied." → "Follow up in a click."** | "Track every application, see how early you applied, and follow up in a click." |
| 25.2–30 s | `S7Cta` | A deep-blue sheet rises over the tracker: white SpotApply wordmark, "Your experience. *Less busywork.*", a white **Start free** button, **app.spotapply.ai**, three recap pills (⚡ Apply today · Grounded resume · Your final click) and a fan of the real screens (tagged "Demo data"). Lands by 27.3 s, then holds still. | "Your experience. Less busywork. Start free with SpotApply." (ends 29.0 s) |

Each scene starts from the exact last frame of the one before, so the
hand-offs read as one piece of UI rather than cuts.

## The 60-second explainer

| | |
|---|---|
| Output | `out/spotapply-explainer-60s.mp4` (composition `SpotApplyExplainer60`, `src/w60/`) |
| Format | 1920×1080, 30 fps, 1800 frames, 60.0 s, H.264 High, yuv420p, BT.709 (limited range) |
| Audio | Voiceover (same voice), AAC 48 kHz. Measured: -13.8 LUFS integrated, -2.0 dBFS true peak. No music. |
| Captions | `out/spotapply-explainer-60s.srt` |
| Poster | `out/spotapply-explainer-60s-poster.png` (frame 540: the fit breakdown) |

A redesign, not a longer cut: widescreen instead of vertical; the message
sits in a left column (a serif chapter number, eyebrow, headline, one line of
support) and the product in a desktop app window on the right, where a
camera pans and pushes in across the real desktop screens (`Focus` rects in
source pixels, `src/w60/components.tsx`); a five-step chapter rail runs
along the bottom. The live screens are the desktop captures in
`public/live60/` (1280×800 @2x, `live-capture/capture_v3.py`).

| Time | Scene | On screen | Voiceover |
|---|---|---|---|
| 0–5.4 s | `W1Problem` (illustrated, dark) | Six forms from six fictional employers fill with the same name and email, then fold into one resume file. **"Another search. Another form."** | "Another job search. More tabs. Another form, asking for the same details." |
| 5.4–11 s | `W2Intro` (illustrated) | A light circle opens from the file: **Meet SpotApply**, the upload card ("Read: 6 years · 4 core skills"), target roles and where (Backend / Platform Engineer, United States, Remote, Open to relocating: Seattle, WA). | "Meet SpotApply. Upload your resume once, and choose the roles and places you want." |
| 11–18.8 s | `W3Find` (**LIVE**) | The board's **Apply today** divider with the Lumen Data card ringed ("⚡ Posted 2 hours ago: apply today"), then its match report: **why it matched** and the **fit breakdown** (skills · experience · location · work auth). **01 Find "Fresh roles that fit." → 02 Understand "See exactly why."** | "It finds fresh roles that fit, and shows you exactly why: skills, experience, location." |
| 18.8–24.2 s | `W4Sponsor` (**LIVE**) | The same report's **sponsorship card**, as a candidate who needs sponsorship sees it (labelled on screen): "Sponsorship not stated … worth a quick ask", with **"Check employer requirements"**. **"Sponsorship, researched."** | "If you need sponsorship, it shows what the posting says. Always check with the employer." |
| 24.2–33.6 s | `W5Tailor` (**LIVE**) | **Tailoring Studio**: the summary's keywords highlighted, **Locked to your resume** (degree, employer, dates never altered), the **cover letter** tab, the **one-page PDF and Word** downloads. **03 Prepare "Tailored, not invented."** | "It tailors your resume and cover letter from what you've actually done. Your degree, employers and dates stay locked." |
| 33.6–43.6 s | `W6Apply` (illustrated form + **LIVE** setting) | The employer's form fills from SpotApply's in-browser panel ("Review the answers, then click Submit yourself."); the person's cursor checks each field and clicks Submit. Then the real **One-click submit** setting, "Off: fill only, I click Submit": **"Off unless you turn it on"**. **04 Review "Filled for you. Sent by you."** | "It fills the form in your browser. You review every answer, and click Submit. Or approve it with one click, if you turn that on." |
| 43.6–50 s | `W7Track` (**LIVE**) | The Submitted tab's **earliness receipt** ("A measured clock, not a promise"), then the **Outreach kit**, reading down to the email draft and its **Gmail / Outlook / Mail app** links ("Drafts only: you send from your own account"). **05 Track "Know how early you applied."** | "Track every application, see how early you applied, and follow up with a ready-made email." |
| 50–53 s | `W8Recap` | **Find. Understand. Prepare. Review. Track.** | (the same five words) |
| 53–60 s | `W9Cta` (dark) | The end card, widescreen: white wordmark, "Your experience. *Less busywork.*", **Start free**, **app.spotapply.ai**, a fan of three real screens (tagged "Demo data"). Lands by 54.3 s, then holds perfectly still for 5.7 s. | "Your experience. Less busywork. Start free with SpotApply." (ends 57.0 s) |

The sponsorship scene is the one place the demo profile changes: Maya Chen
is a US citizen (her card reads "No sponsorship needed"), so the card is shot
with an F-1 OPT profile and the scene says "Shown for a candidate who needs
sponsorship". It is a research signal, never a promise.

## Product truth

The live site is blocked from the build environment, so "live" means the
current code on this branch running locally. Claims follow
`app/templates/landing.html` and the shipped behaviour:

- **The last click is the user's.** By default the user clicks the form's own
  Submit. One-click submit is opt-in: one Apply & Submit click approves that
  one application, on login-free boards, never past a CAPTCHA or sign-in
  (landing: "The last step is your click. Always."). The ad never shows
  anything submitting on its own.
- **The earliness receipt is a measured clock**, not odds. The ad shows the
  receipt; it never claims a better chance of a reply.
- **Outreach is drafts and links.** SpotApply never sends for the user; the
  ad shows the email draft and its send links, never a reply, an interview or
  an offer.
- **Fit score 94** appears as the app shows it, in a scene labelled demo data;
  it is never presented as an interview chance.
- **Sponsorship is never promised.** The 30-second ad leaves it out; the
  explainer shows the card as a research signal with "Check employer
  requirements" beside it.
- **Not shown on purpose:** prices, promotions, testimonials, customer
  counts, interview or offer outcomes. (The LinkedIn drafts that ran past
  300 characters and the GitHub note that claimed open-source activity the
  lookup did not find were app bugs, fixed on this branch; see
  `live-capture/README.md`.)
- **Demo data:** every product scene carries "Demo data · fictional candidate
  and companies". Maya Chen, Brightwave Logistics, Lumen Data, Orchard Systems
  and the rest are fictional; email uses `example.com`; the phone number is
  in the reserved 555-01xx range. Scores and reasoning that normally come from
  the AI were written into the demo seed by hand.
- House spelling: "resume", never "résumé", in anything a viewer reads (the
  voice is told "résumé" only so it says REZ-oo-may; see `voiceover/`).

## Where things live

```
src/
  brand.ts        colours (mirror landing.html :root), fonts, safe area, layout rhythm
  copy.ts         every word on screen
  story.ts        the one candidate and role (must match the live seed)
  timeline.ts     scene lengths + where each voiceover line starts
  SocialAd30.tsx  main composition: background, scenes, demo label, voiceover
  w60/            the 60-second explainer: layout, timeline60, components, W1Problem … W9Cta
  components/     Screen (live captures + focus rings), Headline, Logo, BluePath, Cursor, ...
  scenes/         S1Hook … S7Cta
public/
  live/           the five phone captures the 30-second ad uses
  live60/         the eight desktop captures the explainer uses
  voiceover/      the narration clips: seven for the ad, nine (w_*) for the explainer (-16 LUFS, 48 kHz mono)
  fonts/, brand/  Outfit + Instrument Serif (OFL) and the SpotApply mark
voiceover/        lines.json, synthesis + ASR-check scripts, SRT builder, provenance
live-capture/     run the app locally on fictional data and re-shoot the screens
```

## Assets and licences

| Asset | Source | Licence / status |
|---|---|---|
| Live screens (`public/live/`, `public/live60/`) | The SpotApply app (this repo), fictional demo seed | SpotApply's own UI |
| Narration (`public/voiceover/`) | Kokoro-82M, voice `af_heart`, synthesized locally | Apache-2.0 (model + voices); provenance in `voiceover/README.md` |
| Outfit, Instrument Serif | `@fontsource/*` (npm) | SIL Open Font License 1.1 (`public/fonts/`) |
| SpotApply mark | `app/static/favicon.svg`, redrawn in `Logo.tsx` | SpotApply's own brand asset |
| Icons, illustrated UI, cursor | Drawn in code | Original |
| Music / SFX | none | — |

**Remotion licence:** Remotion is free for individuals and companies of up to
three people; larger for-profit organisations need a company licence
(<https://www.remotion.pro/license>). Check this before commercial use.

Before publishing: confirm the live site still offers **Start free**, and put
the real destination in the platform's CTA or link (a URL in a video is not
clickable). Upload the `.srt` as the platform's caption track.
