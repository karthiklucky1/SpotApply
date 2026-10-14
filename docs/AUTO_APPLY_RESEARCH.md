# Auto-apply: what can be automated, what cannot, and what we built (2026-10-10)

The founder's request: "we need to do auto apply — for that, reCAPTCHAs and
email sign-in — all in detail research, and if you find the best idea try to
implement it." This is that research, written from the code and from what is
known about each applicant-tracking system (ATS). It ends with what was built
tonight, what is deliberately not built, and the decision the founder has to
make.

Nothing in this document could be verified against a live ATS from the build
container: it has no outbound network to job boards. Every claim about an ATS
below is from documentation, prior audits in `docs/research/`, and the
fixtures the extension is tested against. Treat the per-ATS table as a map to
check against real postings, not as measured fact.

## 1. The three walls, and which can be climbed

An application that the user does not click through has to get past three
things. They are different problems and only one of them is engineering.

| Wall | What it is | Can software climb it legitimately? |
|---|---|---|
| **Form filling** | Fields, uploads, screening questions, EEO, multi-page flows | **Yes.** This is what the extension does; the gaps are coverage and accuracy, measured in §4. |
| **Account walls** (Workday, iCIMS, Taleo, SuccessFactors) | Create an account per employer, confirm an email, sign in | **Partly.** The account is the user's: we may prefill the email, hand the password to the browser's own manager, and GUIDE the verification step. We may not store passwords, create accounts unattended, or read a mailbox the user did not open for us. |
| **CAPTCHAs** (reCAPTCHA v2/v3/Enterprise, hCaptcha, Cloudflare Turnstile) | The site's statement that it wants a human present | **No.** Solving or bypassing them means a third-party "solver" farm, browser-fingerprint disguise or token replay. Every one of those breaks the ATS terms, the Chrome Web Store developer policies (deceptive behaviour, circumventing security measures), and the product's own rule. We detect, pause and ask the human. |

The product rule in `CLAUDE.md` — *the human always reviews and clicks Submit,
never auto-submit* — is not a limitation we forgot to lift. It is what keeps
the Chrome extension on the Store, keeps users' ATS accounts from being banned
(Workday and LinkedIn both suspend accounts for automated activity), and keeps
the user responsible for a document that carries their name. "Auto-apply" as
sold by LazyApply or JobCopilot (reviewed in
`docs/research/competitive-analysis-2026-07.md`: LazyApply 2.2/5, "fails 90% of
the time", "invents middle names", applies to the wrong jobs) is the product
category users rate worst. Simplify and Jobright, the two best-rated tools, are
human-in-the-loop copilots — the same shape as ours.

## 2. Per-ATS reality

"Account" = must the applicant sign in to apply. "CAPTCHA" = seen on the apply
form. "Native autofill" = the ATS parses an uploaded resume into its own
fields. "Terms" = what the operator says about automation.

| ATS | Account | CAPTCHA | Native autofill | Terms on automation | Our posture |
|---|---|---|---|---|---|
| Greenhouse (`boards.greenhouse.io`, `job-boards.greenhouse.io`) | No | reCAPTCHA on some boards (employer setting) | "Autofill with resume" on the new board UI | Board ToS forbid bots scraping; filling a form in the applicant's own browser is not addressed | Fill everything, native autofill first when offered, human submits |
| Lever (`jobs.lever.co`) | No | hCaptcha on some postings | Resume parse on upload | Prohibits automated access to the service | Same |
| Ashby (`jobs.ashbyhq.com`) | No | hCaptcha iframe present but invisible until triggered (why `isCaptcha()` checks visibility) | "Autofill application" from resume | Prohibits bots | Same |
| SmartRecruiters | Usually no | Rare | "Autofill from resume / LinkedIn" | Prohibits automation | Same |
| Workable, Teamtailor, Recruitee, BambooHR, Jobvite | No | reCAPTCHA (invisible v3) on some | Varies | Prohibit automation in general terms | Same |
| **Workday** (`*.myworkdayjobs.com`) | **Yes** — per-employer account, email + password, many tenants send a verification code | Rarely a CAPTCHA; Akamai bot management on some tenants | "Autofill with Resume" creates work history and education entries | Terms forbid automated access and account creation | Fill in the user's session after THEY sign in; prefill the account email; guide the code step (§5); never create the account unattended |
| **iCIMS** | Often yes | reCAPTCHA on many tenants | Resume parse | Forbids automation | As Workday |
| **Oracle Taleo**, **SAP SuccessFactors** | Yes | Sometimes | Resume parse (poor) | Forbid automation | As Workday; lowest priority — legacy and shrinking |
| **LinkedIn Easy Apply**, **Indeed Apply** | Yes (their account) | Yes | n/a | **Explicitly ban all automated activity**; accounts are suspended | Never. Discovery links only (already the rule) |

Two conclusions fall out of the table. First, the login-free ATSes
(Greenhouse, Lever, Ashby, SmartRecruiters and the smaller ones) cover most of
the tech postings SpotApply discovers, and on those the only manual step left
after a complete fill is the Submit click — the thing we keep on purpose.
Second, Workday is where the time goes: an account per employer, a
verification email, five or six pages. That is a guidance and prefill problem,
not a bot problem, and it is where the extension's native-autofill support
(§5) and the code-step guidance pay off.

## 3. CAPTCHAs and "email sign-in" in detail

**reCAPTCHA v2** (checkbox / image grid): requires a human. **v3 / Enterprise**:
scores the session invisibly from behaviour and browser signals; a real user in
their own Chrome with the extension scores as a human, because they are one.
The score drops when the browser is automated (`navigator.webdriver`), headless
or fingerprint-patched — which is why the server-side Playwright agent
(`app/autofill/agent.py`, founder-only) used to carry a "stealth" init script.
That script was removed tonight: patching `navigator.webdriver`, plugins and
the WebGL vendor to look like a person is the definition of circumventing a
security measure, and the agent never needed it to fill a form a human then
reviews. The extension path, which every non-founder user takes, never had it
and never will.

**hCaptcha** and **Turnstile** behave the same way: pass silently for an
ordinary browser session, challenge when the session looks automated. The
extension detects a VISIBLE challenge (`isCaptcha()`: recaptcha/hcaptcha
iframes, `.cf-turnstile`, `#challenge-stage`, the "prove you are human" text),
pauses, shows the overlay "Please solve the CAPTCHA, I'll continue
automatically after", and resumes when the form reappears. That is the correct
and only compliant handling. Paid solving services (2Captcha and the like) are
not an option: they violate the ATS terms, the Store policies, and in several
jurisdictions the computer-misuse statutes the hiQ/LinkedIn line of cases left
open; and a job application filed through one is fraud on the employer's
"I am a human" assertion.

**Email sign-in / verification codes** (Workday "Create Account", iCIMS,
Taleo): the employer emails a one-time code; the applicant types it. The
legitimate design is a RELAY the user watches, not an unattended login:

1. The extension recognises the code field (`autocomplete="one-time-code"`,
   or a label matching verification / security / confirmation code) and shows
   a one-line guide naming the sender domain to look for.
2. With the user's existing inbox permission (the same one the "Scan inbox"
   button uses on mail.google.com), a "Fetch code from Gmail" button opens the
   inbox in a background tab, reads only the newest message from that tenant's
   domain received after the page asked, extracts a 6–8 digit code, and offers
   "Paste code". The user clicks it. The mailbox is read once, for one sender,
   for one code; nothing is stored.
3. Passwords are never generated, stored or typed by SpotApply. Chrome's
   password manager offers a strong password and saves it; that is the user's
   credential store, not ours.

Step 1 is cheap and safe and is the next extension change (after tonight's
native-autofill work lands; both edit `extension/content.js`). Step 2 needs a
fixture inbox and an explicit permission prompt in the popup before it ships;
it is designed here so it can be built in a day, not guessed at.

## 4. What "auto-apply" means for us: zero clicks to fill, one click to submit

The honest version of auto-apply is to make the human's Submit the ONLY manual
action, and to make everything before it fast and right. Measured tonight:

| Capability | State | Measurement |
|---|---|---|
| Contact, links, work-auth, screening answers, uploads | Shipped (extension 1.4.0 rules, audit 2026-09-30) | `extension-tests/test_extension_forms.py` harness |
| Voluntary self-identification (EEO) | **Fixed tonight** — the saved answers never reached select/radio/custom-dropdown controls | 66/66 harness scenarios (E1–E12 added), 25 matcher cases in `tests/test_extension_rules.py` |
| Tailored resume attached | Shipped; the fill-pack and attach route refuse a draft the export gate blocks | `test_fill_pack_grounding`, `test_export_gate` |
| ATS native "autofill with resume" | **Built tonight** (see the commit that lands it): detect the widget, feed the resume, wait for the parse to settle, fill only what is empty or wrong, never attach twice | Workday-like and Ashby-like fixtures; cannot be verified against a live tenant from this container |
| CAPTCHA / login wall | Detect, pause, guide, resume | `isCaptcha()`, `isLoginWall()`, the form watcher |
| Verification-code step | Guidance designed (§3), relay designed, not built | — |
| Submit | The user's click, confirmed by the employer's success page or the user's "Yes" | `test_extension_rules` (SUBMIT_ATTEMPTED vs FORM_SUBMITTED) |

Fill accuracy is measured in the harness, not estimated: every scenario is a
real fixture form with the expected value per field. Tonight's EEO work took
the fill set from "demographics skipped on every control that was not a plain
`<select>`" to 66 of 66 scenarios passing. There is no number for live ATS
pages from here; the harness is the lower bound we can stand behind.

## 5. What was built, and what is deliberately not

Built on 2026-10-10:

- **ATS native autofill** in the extension (Workday "Autofill with Resume",
  Ashby/Lever/Greenhouse/SmartRecruiters parse-on-upload): used first when
  present, settled, then the pack fills only empty or conflicting fields. The
  time saved is the multi-entry work history and education that our filler
  cannot create. Kill switch in extension storage; per-ATS allow-list.
- **Stealth script removed** from the server-side agent; the dead
  `_click_submit` helper with it. The agent fills; it does not disguise or
  submit.

Built on 2026-10-11 (founder decision, after reading §4: "if auto submit works
for a few boards, build it there; research how Tsenta handles email login and
CAPTCHAs"):

- **One-click submit — the "Submit from SpotApply" shape, exactly as §5 of the
  10-10 version described it.** `UserProfile.one_click_submit` is OFF by
  default (Settings → Autofill). While it is on, the board's fill button reads
  **Apply & Submit** and asks for confirmation; that click is the user's
  approval of THAT application. The server decides, once, in
  `_one_click_decision` (`app/api/server.py`), and the extension only obeys
  `pack.auto_submit`: the kill switch `ONE_CLICK_SUBMIT_ENABLED`, the setting,
  an aggregator-redirect link (never), the apply URL's host on the login-free
  allow-list `ONE_CLICK_SUBMIT_HOSTS` (greenhouse.io, lever.co, ashbyhq.com,
  smartrecruiters.com — exact host or real subdomain, never a substring), and
  `ONE_CLICK_SUBMIT_DAILY_LIMIT` (25 approved clicks a day, reserved in
  `UserUsage.one_click_submit_count`; a refused click costs nothing). In the
  tab, `maybeAutoSubmit` (`extension/content.js`) runs AFTER the fill, the
  resume attach and the field audit, and refuses — with the reason shown in
  the overlay and sent as `auto_submit_blocked` — when the host is off the
  list, the page is a login wall or shows a CAPTCHA, any required field still
  needs the human or the resume was not acknowledged, no Submit control is
  found inside an application form (a sign-in form's button never counts, nor
  Cancel/Back/Save/Withdraw), the button is disabled, or the browser's own
  validation rejects the form. Otherwise it presses the form's OWN Submit
  button (never `form.submit()`), announced through the same
  `noteSubmitAttempt` path as a human click, and **"Submitted" is still
  written only when the employer's confirmation page appears** (or the user
  answers Yes). Terms §3/§4 and the landing, pricing, README and extension
  pages say so. Guards: `tests/test_one_click_submit.py`, the one-click block
  in `tests/test_extension_rules.py`, harness scenarios OC1–OC8.
- **Login handoff, step 1 of §3.** On a sign-in / create-account page the
  extension fills the account EMAIL (and its "verify email" twin), never a
  password, and reports `login_needed`; a page asking for an emailed code is
  recognised (`verificationCodeField`: `autocomplete="one-time-code"`,
  otp/verification/passcode names, "verification code" labels), left to the
  user and reported as `verification_code_needed`; a visible CAPTCHA is
  reported as `captcha_needed`. Each handoff raises ONE dashboard
  notification, "Your application needs you" (`_notify_needs_you`, 15-minute
  dedup), so a user who switched tabs learns the form is waiting.

Not built, on purpose:

- **CAPTCHA solving or evasion** of any kind. See §1 and §3. One-click submit
  stops at a visible challenge and never retries past it.
- **Unattended account creation or password handling.** The browser's
  password manager is the credential store. The email prefill is the whole of
  our part in a sign-in.
- **Fetching the verification code from the inbox** (§3 step 2). Designed,
  not built: it needs a fixture inbox and an explicit permission prompt in
  the popup first.
- **One-click submit on account-walled ATSes** (Workday, iCIMS, Taleo, Oracle,
  SuccessFactors). The allow-list is the login-free boards only; the account
  step is the human's, and a submit there would mean a stored login.
- **Headless or server-side submission for users.** `autofill_multi_user_enabled`
  stays founder-only, and even there the human submits.

## 6. Recommendation

1. Measure one-click submit for two weeks before widening the allow-list:
   `auto_submit_attempted` vs `auto_submit_blocked` by reason and host
   (FunnelEvent `extension_auto_submit_*`), and the share of attempts that
   reached a confirmation (`/application/{id}/submit` with `how=one_click`).
   A board where most attempts end in `no_submit_button` or `validation`
   needs its form studied, not the gate loosened.
2. Update the Chrome Web Store listing with the new behaviour (manifest 1.2.0,
   packaged 2026-10-12 for review): the one-click submit is opt-in, the user
   approves each application, and the extension never solves a CAPTCHA or
   stores a password. The Terms already say it.
3. Build §3 step 2 (the inbox fetch of a verification code) only with the
   popup permission prompt and a fixture inbox; it is the one remaining
   friction on Workday applies that is ours to remove.
4. Keep saying what the product is: the fastest path from a posting to a
   reviewed, submitted application — not a bot that applies while you sleep.
   The tools that promise the second have the worst ratings in the category
   for a reason, and the employers on the other side know what a bot looks like.

## 7. Research, 2026-10-11: how the category does it, and what the founder asked

Six research angles ran in parallel (Tsenta; ten other auto-apply products;
the login-free ATSes; the account-walled ATSes; verification-code prior art;
Chrome Web Store / ATS-terms / legal), 177 findings, then one synthesis. Every
vendor, policy and ATS help page was read through search snippets or GitHub
mirrors because this container has no egress to them, so quotes may be
truncated and **someone with a browser must re-read the live Chrome Web Store
policy pages and the Workday / Greenhouse / Lever help articles before any of
this is quoted in Terms, the Store listing or marketing**.

**The founder's premise** was "auto-submit works for a few boards and direct
links; Tsenta does it because they handle email login and CAPTCHAs". The first
half holds; the second does not:

- **Tsenta** (tsenta.com, YC S26 — no product called "Treats AI" exists)
  submits from a **cloud browser worker**, not the user's browser; the
  extension only queues postings. Account walls are solved by **credential
  custody**: "we even create your Workday accounts!", with a Tsenta-held
  per-user "application password" whose storage is undisclosed. Verification
  codes come from an **optional read-only inbox OAuth** (its own Gmail app,
  Composio for Outlook) that "temporarily process[es] limited email content
  for the sole purpose of detecting and autofilling one-time passwords". Its
  pre-submit review pause works only on "compatible" systems and its Auto
  Apply mode skips review entirely. **CAPTCHAs are mentioned nowhere** on its
  site, docs, API docs, privacy policy or terms; its blog concedes bots "get
  stuck on a captcha" and its FAQ says it "does not promise that automated
  assistance is undetectable". Receipts are text records; pricing sells volume
  (25 free, $19/600, $39/1,500, $99/4,500 per 30 days).
- **The category** splits into autofill-first extensions where the human
  presses Submit (Simplify ~600k users at 4.86★, Jobright, Careerflow, Huntr —
  the best reputations) and true submitters: Easy-Apply session bots
  (LazyApply, on LinkedIn/Indeed, whose terms ban them) or cloud agents
  (Tsenta, JobCopilot, Sonara, Massive, AIApply, LoopCV) whose reported
  failures are exactly email verification walls (Sonara: 25–50% of submissions
  failed), Workday logins and CAPTCHAs. No vendor publishes how it handles a
  CAPTCHA; LoopCV alone states a posture — "stop and hand back to the human".
  Five rules explain the reputation gradient: the human presses or explicitly
  approves Submit; fail loudly, never guess; charge nothing on failure;
  employer ATS forms, never LinkedIn/Indeed; show exactly what was sent.
- **The login-free ATSes** have **no applicant-side apply API** — Greenhouse,
  Lever, Ashby, SmartRecruiters, Workable, Breezy, BambooHR all need the
  EMPLOYER's key — so one-click submit can only be a trusted click on the HTML
  form in the user's own browser. They are login-free, not challenge-free:
  Greenhouse runs invisible reCAPTCHA with three post-click outcomes (silent
  pass / emailed verification code / visual challenge, employer-tuned); Lever
  uses hCaptcha and may lock the Location field BEFORE Submit; Ashby's spam
  protection scores the whole form and can reject with nothing to solve;
  Workable tags "AI-assisted" applications for recruiters; Teamtailor filters
  after submission. Confirmation-page patterns are documented nowhere.
- **The account-walled ATSes**: Workday needs a per-tenant account only where
  the employer requires it (some run a guest flow), verifies by emailed LINK
  more often than code, and sits behind Akamai bot management on some tenants;
  iCIMS creates the account inline behind invisible hCaptcha; SuccessFactors
  gates account creation with reCAPTCHA or an email OTP; Oracle Recruiting is
  passwordless with a 6-digit code at the END of the flow (5 wrong = 30-minute
  lockout). None of these is ever on the one-click list.
- **OTP prior art**: the only shipped "suggest a code from email" is
  Chromium's Gmail OTP service — field-triggered, consent-gated, minutes-scoped,
  and filled only when the sender's domain relates to the page origin. That
  origin rule matters: Workday mails from myworkday.com while forms sit on
  <tenant>.myworkdayjobs.com, so any SpotApply relay needs an explicit per-ATS
  sender map. Gmail API read scopes are restricted (annual CASA assessment);
  Gmail's DOM moves (2018, 2022×2, 2023, 2024) and an MV3 fix reaches users
  only after Store review.
- **Policy/legal**: the supported shape is exactly ours — the extension, in
  the user's own Chrome, on an allow-list of login-free boards, after an
  explicit per-application approval, with the user as the applicant and party
  to each board's terms. Forbidden or indefensible: CAPTCHA solving, token
  relay or fingerprint disguise (Google ToS "bypassing protective measures",
  Store "facilitate unauthorized access", Developer Agreement §4.4.1 + Repeat
  Abuse, DMCA §1201(a)(2) service liability, a false human attestation to the
  employer); server-side login or stored ATS passwords (the Ryanair v.
  Booking.com logged-in-access pattern); any LinkedIn/Indeed submission.
  Reading mail — DOM or API — is "personal communications" under Store policy:
  prominent disclosure, a Limited Use statement, no human access, no storage.

**Adopted the same day** (on top of §5): the form-aware challenge check
`captchaBlocksSubmit` (the page-level `isCaptcha()` answers false whenever a
form is present, so a checkbox or image challenge INSIDE the form was invisible
to the gate); the **5-second countdown with Stop** before the press, held while
the tab is hidden and cancelled by the user's own click or keystroke
(`submitCountdown`); the post-press watcher's two extra exits — an emailed-code
step or a visual challenge after the press is a handoff, never a retry; the
Submit finder never presses Next/Continue and never a job page's "Apply now";
the submit is bound to the approved posting's own page (`samePosting`); the
same-day **revocation list** `ONE_CLICK_SUBMIT_HOST_BLOCKLIST`, checked before
the allow-list; the click's approval survives the extension's pack refresh but
a real refusal from the server wins; Terms §4 gained the four sentences above
(not a guarantee of receipt, the applicant is the user, never on sign-in sites,
not undetectable). Tier-B hosts (Workable, Jobvite, Recruitee, Breezy,
Pinpoint, Rippling, BambooHR, Teamtailor) are NOT added: the plan's rule is one
host at a time, after ≥50 human-submitted applications on it with the success
page recognised and a visible-challenge rate under 10%.

**Not adopted yet, written down for the founder** (the plan's open questions):

1. A full **review sheet** before Apply & Submit (every value the pack will
   fill, documents, recipient host), built from a `?preview=1` pack that
   reserves no consent — the Store's "confirm content and recipients" rule
   read literally. Today the confirm names the company and the in-tab overlay
   shows the fill audit before the countdown.
2. A per-submission **receipt** (fields as sent, documents + hashes,
   confirmation URL/text, mechanism), an admin view per board token, and
   `auto_submit_confirmed / rejected / cancelled` telemetry split by host.
3. After the user solves a CAPTCHA on a one-click page: who presses Submit?
   Recommended: the user (the site asked for a person).
4. The verification-code **relay** (§3 step 2): GUIDE only until
   `verification_code_needed` volume justifies a click-triggered "Find my code"
   reading the user's OPEN Gmail tab with in-extension consent; the Gmail API
   route (restricted scope + CASA) is likely never worth it.
5. Host permissions (`*://*/*` vs ATS suffixes + activeTab), the Store listing
   and Privacy-practices tab for the next extension release, a trust ramp
   (N manual submissions before one-click can be turned on), plan-based or
   user-set daily caps, and who receives board/employer notices with a
   same-day removal SLA.
