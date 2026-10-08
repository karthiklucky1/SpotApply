# SpotApply — 30-second social ad

A Remotion (React + TypeScript) project that renders a 30-second vertical ad
for SpotApply: **"Your experience. Less busywork."**

| | |
|---|---|
| Output | `out/spotapply-social-30s.mp4` |
| Format | 1080×1920, 30 fps, 900 frames, 30.0 s, H.264 High, yuv420p, BT.709 (limited range) |
| Audio | **None.** The ad is caption-led and fully understandable muted (see [Audio](#audio)). |
| Poster | `out/spotapply-social-30s-poster.png` (frame 318: "Find roles that fit." + match report) |
| Voiceover script | [`VOICEOVER.md`](VOICEOVER.md), timed to the scenes, for a later recorded take |

This folder is separate from the app. Nothing here is imported by, or
deployed with, the SpotApply service (`/.dockerignore` excludes it).

## Preview and render

```bash
cd spotapply-ad
npm install

npm run dev          # Remotion Studio: scrub the ad and each scene
npm run render       # → out/spotapply-social-30s.mp4
npm run poster       # → out/spotapply-social-30s-poster.png
npm run frames       # → out/frames/ (one PNG per scene + transitions, for review)
npm run lint         # eslint + tsc
```

The exact render command behind `npm run render`:

```bash
npx remotion render SpotApplySocial30 out/spotapply-social-30s.mp4 \
  --codec=h264 --crf=16 --x264-preset=slow --image-format=png --color-space=bt709
```

Remotion downloads its own headless Chrome on the first render. If that
download is blocked, point it at an existing headless Chromium:
`REMOTION_BROWSER_EXECUTABLE=/path/to/headless_shell npm run render`.

## The story (one candidate, one role, one application)

| Time | Scene (`src/scenes/`) | On screen |
|---|---|---|
| 0–3 s | `S1Hook` | Three overlapping forms get the same name and email typed into them, then stop. **"Still typing it all again?"** |
| 3–6 s | `S2Reveal` | The forms fold into one resume, which opens as a profile. **"Meet SpotApply"** (wordmark on screen by ~3.5 s). |
| 6–11 s | `S3Find` | The real board screenshot appears for the candidate; a blue thread picks the top role, which opens into its match report with three written reasons: skills, experience, location. **"Find roles that fit."** |
| 11–16 s | `S4Tailor` | The summary is reworded from facts already in the resume; the words it brings forward light up where they came from; employer, dates and degree are marked **Unchanged**; "Checked against your original resume". **"Your experience. Clearly presented."** |
| 16–22 s | `S5Apply` | The employer's form fills in the browser and the tailored resume drops into its field ("Filled by SpotApply. Review before you submit."). Then the person's cursor, labelled **You**, checks each field and clicks Submit. **"Less retyping."** → **"Review. Then submit."** |
| 22–25 s | `S6Track` | The same role moves from Shortlisted to Submitted. **"Keep your search organized."** |
| 25–30 s | `S7Cta` | SpotApply, "Your experience. *Less busywork.*", **Start free**, **app.spotapply.ai**. Everything lands by 26.7 s; the last 3.3 s are completely still. |

Each scene starts from the exact last frame of the one before (the next
scene re-renders the previous scene's final UI and transforms it), so the
hand-offs read as one piece of UI rather than cuts.

## Product truth: what was checked and what it is based on

The live site (`app.spotapply.ai`) could not be fetched from the build
environment (its network policy blocks the host). The ad is therefore based on
the **landing page source in this repository** (`app/templates/landing.html`)
and the app's own public screenshots (`app/static/shots/`). Confirm before
publishing that the live page still matches. Specifically:

- **Shown, and present on the landing page:** job discovery, match scoring with
  written reasons, grounded resume tailoring ("Every changed line is then
  checked back against your original"), autofill in the user's own browser,
  "You review everything and click Submit yourself", application tracking,
  the **Start free** CTA.
- **Deliberately not shown:** unattended submission, interviews, offers or
  recruiter replies, guaranteed sponsorship, prices or promotions, customer
  counts, testimonials, time-saved claims, sponsorship (the 60 s cut covers
  it), cover letters (they appear on the real board's button label, but no
  scene demonstrates one).
- **The fit score (94)** is labelled "Fit score 94/100 · demo data" and is
  never presented as an interview chance.
- **Demo data:** every illustrated product scene carries "Demo data · fictional
  candidate and companies". Maya Chen, Brightwave Logistics, Lumen Data and
  the other companies are fictional (the same fictional companies as the
  public screenshots). Email uses the reserved `example.com` domain; the phone
  number is in the reserved 555-01xx range.
- **Tailoring stays grounded:** `src/story.ts` holds the base resume. The
  tailored summary only uses its title, its dates (2020 to Present = six
  years) and its bullets (FastAPI APIs, Kafka pipeline, Python skill).
- House spelling: "resume", never "résumé", in anything on screen.

## Where things live

```
src/
  brand.ts      colours (mirrors landing.html :root), fonts, safe area, layout rhythm
  copy.ts       every word on screen
  story.ts      the one candidate, base resume, role and other fictional companies
  timeline.ts   scene lengths (frames)
  SocialAd30.tsx  main composition: background + scenes + demo-data label
  Root.tsx      registers the ad plus each scene on its own (Studio › Scenes)
  components/   Headline, Logo, BluePath (the recurring cue), Cursor, DemoLabel, ui
  scenes/       S1Hook … S7Cta
public/
  fonts/        Outfit + Instrument Serif (woff2) and their licences
  brand/        spotapply-mark.svg (copy of app/static/favicon.svg)
  shots/        qualified-board-narrow.png (copy of the app's public screenshot)
```

Layout follows a vertical safe area of 120 px sides, 220 px top, 320 px
bottom. All essential text sits inside it; check the target platform's current
overlays before publishing.

## Assets and licences

| Asset | Source | Licence / status |
|---|---|---|
| Outfit (400–800) | `@fontsource/outfit` (npm) | SIL Open Font License 1.1 (`public/fonts/OFL-Outfit.txt`) |
| Instrument Serif italic | `@fontsource/instrument-serif` (npm) | SIL Open Font License 1.1 (`public/fonts/OFL-InstrumentSerif.txt`) |
| SpotApply mark | `app/static/favicon.svg` (this repo), redrawn inline in `Logo.tsx` | SpotApply's own brand asset |
| Board screenshot | `app/static/shots/qualified-board-narrow.png` (this repo) | SpotApply's own public screenshot; fictional demo data |
| Icons, UI, cursor | Drawn in code | Original |
| Audio | none | — |

**Remotion licence:** Remotion is free for individuals and companies of up to
three people; larger for-profit organisations need a company licence
(<https://www.remotion.pro/license>). Check this before commercial use.

## Audio

No narration, music or sound effects are included. No voice tool or licensed
music was available, and none was purchased or synthesised. The video stands
on its own without sound. To add narration later, record
[`VOICEOVER.md`](VOICEOVER.md) and drop it in with `<Audio>` from
`@remotion/media`; generate captions from the recording itself rather than
from the script.

The URL in the video is not clickable: put the real destination in the
platform's CTA button, post link or profile link.
