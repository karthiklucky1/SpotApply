/// <reference types="expo/types" />

// Committed replacement for the generated (gitignored) expo-env.d.ts so
// `tsc --noEmit` works in a fresh clone before `expo start` has ever run.

declare namespace NodeJS {
  interface ProcessEnv {
    EXPO_PUBLIC_API_URL?: string;
    EXPO_PUBLIC_SUPABASE_URL?: string;
    EXPO_PUBLIC_SUPABASE_ANON_KEY?: string;
  }
}
