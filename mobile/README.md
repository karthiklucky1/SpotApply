# SpotApply Mobile

Expo (React Native) app for iOS **and** Android from one TypeScript codebase.
It is a client of the existing SpotApply backend — it signs in with the same
Supabase Auth project as the web app and calls the same `/api/*` JSON
endpoints with a `Authorization: Bearer <JWT>` header. **No backend changes
were needed.**

## What v1 covers

- **Sign in / sign up** — Supabase email + password (same accounts as the web).
- **Jobs** — scored feed with *Top matches / Fresh / All* filters, search,
  infinite scroll, pull-to-refresh.
- **Match details** — fit score, priority, hire probability, the AI's
  "why this match" reasoning, open the posting, verify it's still live.
- **Pipeline** — outcome funnel (applied → interview → offer, response rate,
  ghosted) plus per-stage lists.
- **Alerts** — the same notifications as the dashboard bell, with mark-read.
- **Profile** — résumé status, plan usage meters, target-roles editor
  (saving triggers instant job adoption server-side), sign out.

Deliberate non-goals on mobile: tailoring, autofill and résumé upload stay on
the web dashboard/extension — the app links out for those. As everywhere in
SpotApply, **the human always clicks Submit**.

## Run it (development)

```bash
cd mobile
npm install
cp .env.example .env   # fill in Supabase URL + anon key
npx expo start
```

Scan the QR code with the **Expo Go** app (App Store / Play Store) — no Mac,
Xcode, or Android Studio needed for development. Press `a`/`i` in the terminal
to target a connected emulator/simulator if you have one.

Pointing at a local backend? Set `EXPO_PUBLIC_API_URL` to your machine's LAN
IP (e.g. `http://192.168.1.20:8000`) — the phone can't reach `localhost`.

## Configuration

| Env var | Meaning |
| --- | --- |
| `EXPO_PUBLIC_API_URL` | SpotApply backend base URL (default `https://app.spotapply.ai`) |
| `EXPO_PUBLIC_SUPABASE_URL` | Supabase project URL (same as web) |
| `EXPO_PUBLIC_SUPABASE_ANON_KEY` | Supabase anon key (public by design) |

`EXPO_PUBLIC_*` values are inlined into the bundle at build time — restart
`expo start` after changing them. Never put the service-role key here.

## Store builds (EAS)

[EAS Build](https://docs.expo.dev/build/introduction/) compiles both platforms
in Expo's cloud — **iOS builds do not require a Mac**:

```bash
npm install -g eas-cli
eas login
eas build:configure          # one-time: creates eas.json, sets projectId
eas build --platform all --profile production
eas submit                   # upload to App Store / Play Store
```

Set the env vars for production builds via `eas env` (or `eas.json` build
profiles) so the store binaries ship with the right Supabase/API values.
Bundle IDs are already set in `app.json` (`ai.spotapply.app`); replace the
placeholder icon/splash art in `assets/images/` before submitting.

## Code map

```
src/
  app/                 # expo-router routes
    _layout.tsx        # auth-protected Stack (Stack.Protected on session)
    sign-in.tsx        # public auth screens
    sign-up.tsx
    (tabs)/            # Jobs / Pipeline / Alerts / Profile
    job/[id].tsx       # match detail (reads the job cache)
  lib/
    config.ts          # EXPO_PUBLIC_* env
    supabase.ts        # supabase-js client (AsyncStorage session persistence)
    auth-context.tsx   # session provider driving the route guards
    api.ts             # typed fetch wrapper + one function per endpoint
    types.ts           # response shapes of app/api/server.py
    job-cache.ts       # in-memory job store (backend has no GET /api/jobs/{id})
  components/          # JobCard, ScoreBadge, StatusPill, Chip, Section, ...
  constants/theme.ts   # brand palette (emerald/indigo), score-band colors
  hooks/               # use-theme, use-api (load/refresh/stale-guard)
```

Conventions:

- **Every new endpoint call goes through `src/lib/api.ts`** with its response
  shape in `types.ts` — keep the server's field names verbatim.
- Screens never import `supabase` for data — auth state comes from
  `useAuth()`, data from `api.ts`.
- The detail screen renders from `job-cache.ts`; any list response must pass
  through `rememberJobs()` first.
- Theme: use `useTheme()` colors — no hardcoded hex in screens. Light/dark
  both supported.
