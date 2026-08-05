import "react-native-url-polyfill/auto";

import AsyncStorage from "@react-native-async-storage/async-storage";
import { createClient } from "@supabase/supabase-js";
import { AppState, Platform } from "react-native";

import { AUTH_CONFIGURED, SUPABASE_ANON_KEY, SUPABASE_URL } from "./config";

// Placeholder values keep module import from throwing when env vars are absent;
// the sign-in screen surfaces the misconfiguration instead (AUTH_CONFIGURED).
export const supabase = createClient(
  SUPABASE_URL || "https://placeholder.supabase.co",
  SUPABASE_ANON_KEY || "public-anon-key",
  {
    auth: {
      storage: AsyncStorage,
      autoRefreshToken: true,
      persistSession: true,
      detectSessionInUrl: false,
    },
  },
);

// Supabase RN guidance: only refresh tokens while the app is foregrounded.
if (Platform.OS !== "web" && AUTH_CONFIGURED) {
  AppState.addEventListener("change", (state) => {
    if (state === "active") {
      supabase.auth.startAutoRefresh();
    } else {
      supabase.auth.stopAutoRefresh();
    }
  });
}
