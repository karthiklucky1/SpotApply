# Voiceover script: 30-second ad (v2)

The rendered MP4 carries a synthetic narration of exactly these lines
(Kokoro-82M, voice `af_heart`; see `voiceover/README.md`). To replace it with
a human take, record the same lines to the same slots and drop the WAVs into
`public/voiceover/` (ids below), then update `frames` in `src/timeline.ts`.

| Starts | Id | Line |
|---|---|---|
| 0.07 s | `hook` | Still typing the same details into every application? |
| 3.40 s | `reveal` | Meet SpotApply. |
| 5.80 s | `find` | It finds fresh roles that fit your experience, and shows you why. |
| 10.20 s | `tailor` | Then it tailors your resume, using only what you've actually done. |
| 14.60 s | `apply` | It fills the form in your browser. You check it, and the final click is always yours. |
| 20.40 s | `track` | Track every application, see how early you applied, and follow up in a click. |
| 25.40 s | `cta` | Your experience. Less busywork. Start free with SpotApply. |

68 words. The narration ends at 29.0 s so the last second is quiet.
Before publishing, confirm the live site still offers **Start free**.

# Voiceover script: 60-second explainer

Same voice and pipeline; ids are `w_<id>` in `public/voiceover/`, placement in
`src/w60/timeline60.ts` (`VOICEOVER60`), text in `voiceover/lines60.json`.

| Starts | Id | Line |
|---|---|---|
| 0.20 s | `w_problem` | Another job search. More tabs. Another form, asking for the same details. |
| 5.67 s | `w_intro` | Meet SpotApply. Upload your resume once, and choose the roles and places you want. |
| 11.33 s | `w_find` | It finds fresh roles that fit, and shows you exactly why: skills, experience, location. |
| 19.00 s | `w_sponsor` | If you need sponsorship, it shows what the posting says. Always check with the employer. |
| 24.47 s | `w_tailor` | It tailors your resume and cover letter from what you've actually done. Your degree, employers and dates stay locked. |
| 33.93 s | `w_apply` | It fills the form in your browser. You review every answer, and click Submit. Or approve it with one click, if you turn that on. |
| 43.87 s | `w_track` | Track every application, see how early you applied, and follow up with a ready-made email. |
| 50.13 s | `w_recap` | Find. Understand. Prepare. Review. Track. |
| 53.33 s | `w_cta` | Your experience. Less busywork. Start free with SpotApply. |

127 words. The narration ends at 57.0 s, inside the end card's 5.7-second hold.
