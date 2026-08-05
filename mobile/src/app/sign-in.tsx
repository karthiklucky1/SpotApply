import { router } from "expo-router";
import { useState } from "react";
import {
  ActivityIndicator,
  Alert,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { BrandHeader } from "@/components/brand-header";
import { Field } from "@/components/field";
import { useTheme } from "@/hooks/use-theme";
import { API_URL, AUTH_CONFIGURED } from "@/lib/config";
import { supabase } from "@/lib/supabase";

export default function SignInScreen() {
  const { colors } = useTheme();
  const insets = useSafeAreaInsets();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const signIn = async () => {
    if (!email.trim() || !password) {
      setError("Enter your email and password.");
      return;
    }
    setBusy(true);
    setError(null);
    const { error: err } = await supabase.auth.signInWithPassword({
      email: email.trim(),
      password,
    });
    setBusy(false);
    if (err) setError(err.message);
    // Success: onAuthStateChange flips the Protected guard and the router
    // moves to (tabs) automatically.
  };

  const forgotPassword = async () => {
    if (!email.trim()) {
      setError("Enter your email above first, then tap Forgot password.");
      return;
    }
    const { error: err } = await supabase.auth.resetPasswordForEmail(email.trim(), {
      redirectTo: `${API_URL}/auth`,
    });
    if (err) setError(err.message);
    else Alert.alert("Check your inbox", "We sent you a password-reset link.");
  };

  return (
    <KeyboardAvoidingView
      style={[styles.flex, { backgroundColor: colors.background }]}
      behavior={Platform.OS === "ios" ? "padding" : undefined}>
      <ScrollView
        contentContainerStyle={[
          styles.container,
          { paddingTop: insets.top + 64, paddingBottom: insets.bottom + 32 },
        ]}
        keyboardShouldPersistTaps="handled">
        <BrandHeader />

        {!AUTH_CONFIGURED ? (
          <View style={[styles.notice, { backgroundColor: colors.warningSoft }]}>
            <Text style={[styles.noticeText, { color: colors.warning }]}>
              Auth isn't configured. Set EXPO_PUBLIC_SUPABASE_URL and
              EXPO_PUBLIC_SUPABASE_ANON_KEY in mobile/.env, then restart Expo.
            </Text>
          </View>
        ) : null}

        <View style={styles.form}>
          <Field
            label="Email"
            value={email}
            onChangeText={setEmail}
            autoCapitalize="none"
            autoComplete="email"
            keyboardType="email-address"
            placeholder="you@example.com"
          />
          <Field
            label="Password"
            value={password}
            onChangeText={setPassword}
            secureTextEntry
            autoComplete="password"
            placeholder="••••••••"
          />

          {error ? <Text style={[styles.error, { color: colors.danger }]}>{error}</Text> : null}

          <Pressable
            onPress={signIn}
            disabled={busy}
            style={({ pressed }) => [
              styles.button,
              { backgroundColor: colors.accent, opacity: pressed || busy ? 0.8 : 1 },
            ]}>
            {busy ? (
              <ActivityIndicator color="#FFFFFF" />
            ) : (
              <Text style={styles.buttonText}>Sign in</Text>
            )}
          </Pressable>

          <Pressable onPress={forgotPassword} hitSlop={8}>
            <Text style={[styles.link, { color: colors.textSecondary }]}>Forgot password?</Text>
          </Pressable>
        </View>

        <View style={styles.footer}>
          <Text style={{ color: colors.textSecondary }}>New to SpotApply?</Text>
          <Pressable onPress={() => router.replace("/sign-up")} hitSlop={8}>
            <Text style={[styles.link, { color: colors.accentText, fontWeight: "700" }]}>
              Create an account
            </Text>
          </Pressable>
        </View>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  container: {
    flexGrow: 1,
    paddingHorizontal: 24,
    gap: 32,
  },
  notice: {
    borderRadius: 12,
    padding: 12,
  },
  noticeText: {
    fontSize: 13,
    lineHeight: 18,
  },
  form: {
    gap: 16,
  },
  error: {
    fontSize: 13,
    fontWeight: "600",
  },
  button: {
    borderRadius: 12,
    paddingVertical: 14,
    alignItems: "center",
    justifyContent: "center",
  },
  buttonText: {
    color: "#FFFFFF",
    fontSize: 16,
    fontWeight: "700",
  },
  link: {
    fontSize: 14,
    textAlign: "center",
  },
  footer: {
    flexDirection: "row",
    justifyContent: "center",
    gap: 6,
    marginTop: "auto",
  },
});
