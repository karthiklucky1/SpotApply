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

## 5. What was built tonight, and what is deliberately not

Built:

- **ATS native autofill** in the extension (Workday "Autofill with Resume",
  Ashby/Lever/Greenhouse/SmartRecruiters parse-on-upload): used first when
  present, settled, then the pack fills only empty or conflicting fields. The
  time saved is the multi-entry work history and education that our filler
  cannot create. Kill switch in extension storage; per-ATS allow-list.
- **Stealth script removed** from the server-side agent; the dead
  `_click_submit` helper with it. The agent fills; it does not disguise or
  submit.
- **CAPTCHA and login handling** reviewed and kept: pause and guide. No
  change needed.

Not built, on purpose:

- **CAPTCHA solving or evasion** of any kind. See §1 and §3.
- **Unattended account creation or password handling.** The browser's
  password manager is the credential store.
- **Headless or server-side submission for users.** `autofill_multi_user_enabled`
  stays founder-only, and even there the human submits.
- **Auto-submit.** Not even as an opt-in flag. A flag defaulting OFF is still a
  flag the Store reviewer reads and the Terms would have to disclose, and the
  one thing every ATS operator can point to. If the founder wants to revisit
  this for the login-free, CAPTCHA-free boards (Greenhouse, Lever, Ashby),
  the honest shape is "Submit from SpotApply": the user clicks Submit on the
  REVIEW screen in the dashboard, having seen the exact field values and
  documents, and the extension presses the form's button in their open tab.
  That keeps the user's click as the submit, moves it earlier, and needs:
  Terms and Store listing updates, a per-user daily cap, a refusal whenever a
  required field is empty, a CAPTCHA is visible or the page is a login wall,
  and an audit row per submission. It is a product decision, not a code one,
  so it is written down here and not in `content.js`.

## 6. Recommendation

1. Ship tonight's work (native autofill, EEO, stealth removal) and measure the
   Workday flow on real tenants with the harness telemetry
   (`native_autofill` in the DO_FILL outcome).
2. Build the verification-code GUIDE (§3 step 1) next; it is an hour of
   extension work and removes the most confusing moment of a Workday apply.
3. Decide on "Submit from SpotApply" with the Terms in front of you. The code
   path is small; the policy exposure is the whole product.
4. Keep saying what the product is: the fastest path from a posting to a
   reviewed, submitted application — not a bot that applies while you sleep.
   The tools that promise the second have the worst ratings in the category
   for a reason, and the employers on the other side know what a bot looks like.
