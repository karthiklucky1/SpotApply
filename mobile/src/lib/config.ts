/**
 * Runtime configuration, inlined at build time from EXPO_PUBLIC_* env vars.
 * Copy .env.example to .env and fill in your Supabase project values —
 * the same SUPABASE_URL / SUPABASE_ANON_KEY the web app uses.
 */

export const API_URL = (process.env.EXPO_PUBLIC_API_URL ?? "https://app.spotapply.ai").replace(/\/+$/, "");

export const SUPABASE_URL = process.env.EXPO_PUBLIC_SUPABASE_URL ?? "";

export const SUPABASE_ANON_KEY = process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY ?? "";

/** False when the Supabase env vars are missing — sign-in screen explains setup. */
export const AUTH_CONFIGURED = Boolean(SUPABASE_URL && SUPABASE_ANON_KEY);
