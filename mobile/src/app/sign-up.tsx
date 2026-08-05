import { router } from "expo-router";
import { useState } from "react";
import {
  ActivityIndicator,
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
import { API_URL } from "@/lib/config";
import { supabase } from "@/lib/supabase";

export default function SignUpScreen() {
  const { colors } = useTheme();
  const insets = useSafeAreaInsets();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [awaitingConfirm, setAwaitingConfirm] = useState(false);

  const signUp = async () => {
    if (!email.trim() || !password) {
      setError("Enter an email and a password.");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    if (password !== confirm) {
      setError("Passwords don't match.");
      return;
    }
    setBusy(true);
    setError(null);
    const { data, error: err } = await supabase.auth.signUp({
      email: email.trim(),
      password,
      options: { emailRedirectTo: `${API_URL}/auth` },
    });
    setBusy(false);
    if (err) {
      setError(err.message);
      return;
    }
    // With email confirmation enabled Supabase returns no session yet.
    if (!data.session) setAwaitingConfirm(true);
    // Otherwise the Protected guard flips and (tabs) loads automatically.
  };

  if (awaitingConfirm) {
    return (
      <View
        style={[
          styles.flex,
          styles.confirmWrap,
          { backgroundColor: colors.background, paddingTop: insets.top },
        ]}>
        <BrandHeader />
        <View style={[styles.notice, { backgroundColor: colors.accentSoft }]}>
          <Text style={[styles.noticeText, { color: colors.accentText }]}>
            Almost there — we sent a confirmation link to {email.trim()}. Confirm your email, then
            come back and sign in.
          </Text>
        </View>
        <Pressable onPress={() => router.replace("/sign-in")} hitSlop={8}>
          <Text style={[styles.link, { color: colors.accentText, fontWeight: "700" }]}>
            Back to sign in
          </Text>
        </Pressable>
      </View>
    );
  }

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
            autoComplete="new-password"
            placeholder="At least 8 characters"
          />
          <Field
            label="Confirm password"
            value={confirm}
            onChangeText={setConfirm}
            secureTextEntry
            autoComplete="new-password"
            placeholder="Repeat your password"
          />

          {error ? <Text style={[styles.error, { color: colors.danger }]}>{error}</Text> : null}

          <Pressable
            onPress={signUp}
            disabled={busy}
            style={({ pressed }) => [
              styles.button,
              { backgroundColor: colors.accent, opacity: pressed || busy ? 0.8 : 1 },
            ]}>
            {busy ? (
              <ActivityIndicator color="#FFFFFF" />
            ) : (
              <Text style={styles.buttonText}>Create account</Text>
            )}
          </Pressable>
        </View>

        <View style={styles.footer}>
          <Text style={{ color: colors.textSecondary }}>Already have an account?</Text>
          <Pressable onPress={() => router.replace("/sign-in")} hitSlop={8}>
            <Text style={[styles.link, { color: colors.accentText, fontWeight: "700" }]}>
              Sign in
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
  confirmWrap: {
    justifyContent: "center",
    paddingHorizontal: 24,
    gap: 24,
  },
  notice: {
    borderRadius: 12,
    padding: 16,
  },
  noticeText: {
    fontSize: 14,
    lineHeight: 20,
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
