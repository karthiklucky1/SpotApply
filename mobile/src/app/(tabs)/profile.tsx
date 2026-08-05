import { Ionicons } from "@expo/vector-icons";
import * as WebBrowser from "expo-web-browser";
import Constants from "expo-constants";
import { useEffect, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { Chip } from "@/components/chip";
import { Section } from "@/components/section";
import { useTheme } from "@/hooks/use-theme";
import { useApi } from "@/hooks/use-api";
import {
  getProfile,
  getResumeStatus,
  getTargetRoles,
  getUsage,
  updateTargetRoles,
} from "@/lib/api";
import { API_URL } from "@/lib/config";
import { useAuth } from "@/lib/auth-context";
import { initials } from "@/lib/format";

function Meter({
  label,
  used,
  limit,
}: {
  label: string;
  used: number;
  limit: number | null;
}) {
  const { colors } = useTheme();
  const pct = limit ? Math.min(100, (used / limit) * 100) : 0;
  return (
    <View style={styles.meter}>
      <View style={styles.meterHeader}>
        <Text style={[styles.meterLabel, { color: colors.textSecondary }]}>{label}</Text>
        <Text style={[styles.meterValue, { color: colors.text }]}>
          {used}
          {limit !== null ? ` / ${limit}` : ""}
        </Text>
      </View>
      {limit !== null ? (
        <View style={[styles.meterTrack, { backgroundColor: colors.neutralSoft }]}>
          <View
            style={[
              styles.meterFill,
              {
                width: `${Math.max(2, pct)}%`,
                backgroundColor: pct >= 100 ? colors.danger : colors.accent,
              },
            ]}
          />
        </View>
      ) : null}
    </View>
  );
}

export default function ProfileScreen() {
  const { colors } = useTheme();
  const { session, signOut } = useAuth();

  const query = useApi(async () => {
    const [profile, usage, roles, resume] = await Promise.all([
      getProfile(),
      getUsage(),
      getTargetRoles(),
      getResumeStatus(),
    ]);
    return { profile, usage, roles, resume };
  }, []);

  const [roles, setRoles] = useState<string[]>([]);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [roleInput, setRoleInput] = useState("");
  const [dirty, setDirty] = useState(false);
  const [savingRoles, setSavingRoles] = useState(false);

  useEffect(() => {
    if (query.data) {
      setRoles(query.data.roles.roles);
      setSuggestions(query.data.roles.suggestions);
      setDirty(false);
    }
  }, [query.data]);

  const addRole = (role: string) => {
    const cleaned = role.trim();
    if (!cleaned) return;
    if (roles.some((r) => r.toLowerCase() === cleaned.toLowerCase())) return;
    setRoles([...roles, cleaned]);
    setSuggestions(suggestions.filter((s) => s.toLowerCase() !== cleaned.toLowerCase()));
    setRoleInput("");
    setDirty(true);
  };

  const removeRole = (role: string) => {
    setRoles(roles.filter((r) => r !== role));
    setDirty(true);
  };

  const saveRoles = async () => {
    setSavingRoles(true);
    try {
      const res = await updateTargetRoles(roles);
      setRoles(res.roles);
      setDirty(false);
      Alert.alert("Saved", "Matching jobs are being pulled in for your new roles now.");
    } catch (e) {
      Alert.alert("Couldn't save roles", e instanceof Error ? e.message : "Try again later.");
    } finally {
      setSavingRoles(false);
    }
  };

  const confirmSignOut = () => {
    Alert.alert("Sign out", "You can sign back in any time.", [
      { text: "Cancel", style: "cancel" },
      { text: "Sign out", style: "destructive", onPress: () => signOut() },
    ]);
  };

  const p = query.data?.profile;
  const usage = query.data?.usage;
  const hasResume = query.data?.resume.has_resume;
  const name = [p?.first_name, p?.last_name].filter(Boolean).join(" ");
  const email = p?.email || session?.user.email || "";

  if (query.loading) {
    return (
      <View style={[styles.flex, styles.center, { backgroundColor: colors.background }]}>
        <ActivityIndicator color={colors.accent} size="large" />
      </View>
    );
  }

  return (
    <ScrollView
      style={[styles.flex, { backgroundColor: colors.background }]}
      contentContainerStyle={styles.content}
      refreshControl={
        <RefreshControl
          refreshing={query.refreshing}
          onRefresh={query.refresh}
          tintColor={colors.accent}
        />
      }>
      <View style={styles.identity}>
        <View style={[styles.avatar, { backgroundColor: colors.accentSoft }]}>
          <Text style={[styles.avatarText, { color: colors.accentText }]}>
            {initials(p?.first_name, p?.last_name, (email[0] ?? "?").toUpperCase())}
          </Text>
        </View>
        <View style={styles.identityText}>
          <Text style={[styles.name, { color: colors.text }]} numberOfLines={1}>
            {name || "Your profile"}
          </Text>
          {email ? (
            <Text style={[styles.email, { color: colors.textSecondary }]} numberOfLines={1}>
              {email}
            </Text>
          ) : null}
          {p?.current_title ? (
            <Text style={[styles.email, { color: colors.textTertiary }]} numberOfLines={1}>
              {p.current_title}
            </Text>
          ) : null}
        </View>
      </View>

      {query.error ? (
        <Section>
          <Text style={{ color: colors.danger }}>{query.error}</Text>
        </Section>
      ) : null}

      <Section title="Résumé">
        <View style={styles.resumeRow}>
          <Ionicons
            name={hasResume ? "checkmark-circle" : "alert-circle-outline"}
            size={20}
            color={hasResume ? colors.accent : colors.warning}
          />
          <Text style={[styles.resumeText, { color: colors.text }]}>
            {hasResume ? "Résumé on file — matching is live" : "No résumé yet — matching is paused"}
          </Text>
        </View>
        <Pressable
          onPress={() => WebBrowser.openBrowserAsync(`${API_URL}/dashboard`)}
          hitSlop={8}>
          <Text style={[styles.webLink, { color: colors.accentText }]}>
            {hasResume ? "Update it on the web dashboard →" : "Upload on the web dashboard →"}
          </Text>
        </Pressable>
      </Section>

      {usage ? (
        <Section title={`Plan — ${usage.plan}`}>
          <Meter label="Tailored docs today" used={usage.tailor_used} limit={usage.tailor_daily_limit} />
          <Meter
            label="Autofills this week"
            used={usage.autofill_used_week}
            limit={usage.autofill_weekly_limit}
          />
          {usage.trial?.active ? (
            <Text style={[styles.trial, { color: colors.accentText }]}>
              Founding trial: {usage.trial.remaining} of {usage.trial.jobs_quota} boosted jobs left
            </Text>
          ) : null}
        </Section>
      ) : null}

      <Section title="Target roles">
        <View style={styles.rolesWrap}>
          {roles.map((role) => (
            <Pressable
              key={role}
              onPress={() => removeRole(role)}
              style={[styles.roleChip, { backgroundColor: colors.accentSoft }]}>
              <Text style={[styles.roleChipText, { color: colors.accentText }]}>{role}</Text>
              <Ionicons name="close" size={14} color={colors.accentText} />
            </Pressable>
          ))}
          {roles.length === 0 ? (
            <Text style={{ color: colors.textTertiary, fontSize: 13 }}>
              Add the roles you want SpotApply hunting for.
            </Text>
          ) : null}
        </View>

        <View style={styles.roleInputRow}>
          <TextInput
            value={roleInput}
            onChangeText={setRoleInput}
            onSubmitEditing={() => addRole(roleInput)}
            placeholder="Add a role, e.g. Backend Engineer"
            placeholderTextColor={colors.textTertiary}
            style={[
              styles.roleInput,
              {
                backgroundColor: colors.inputBackground,
                borderColor: colors.border,
                color: colors.text,
              },
            ]}
            returnKeyType="done"
          />
          <Pressable
            onPress={() => addRole(roleInput)}
            style={[styles.roleAdd, { backgroundColor: colors.accent }]}>
            <Ionicons name="add" size={20} color="#FFFFFF" />
          </Pressable>
        </View>

        {suggestions.length > 0 ? (
          <View style={styles.rolesWrap}>
            {suggestions.slice(0, 6).map((s) => (
              <Chip key={s} label={`+ ${s}`} onPress={() => addRole(s)} />
            ))}
          </View>
        ) : null}

        {dirty ? (
          <Pressable
            onPress={saveRoles}
            disabled={savingRoles}
            style={({ pressed }) => [
              styles.saveButton,
              { backgroundColor: colors.accent, opacity: pressed || savingRoles ? 0.8 : 1 },
            ]}>
            {savingRoles ? (
              <ActivityIndicator color="#FFFFFF" size="small" />
            ) : (
              <Text style={styles.saveButtonText}>Save roles</Text>
            )}
          </Pressable>
        ) : null}
      </Section>

      <Section>
        <Pressable
          onPress={() => WebBrowser.openBrowserAsync(`${API_URL}/dashboard`)}
          style={styles.linkRow}>
          <Ionicons name="globe-outline" size={18} color={colors.textSecondary} />
          <Text style={[styles.linkRowText, { color: colors.text }]}>Open web dashboard</Text>
          <Ionicons name="chevron-forward" size={16} color={colors.textTertiary} />
        </Pressable>
        <Pressable
          onPress={() => WebBrowser.openBrowserAsync(`${API_URL}/privacy`)}
          style={styles.linkRow}>
          <Ionicons name="lock-closed-outline" size={18} color={colors.textSecondary} />
          <Text style={[styles.linkRowText, { color: colors.text }]}>Privacy policy</Text>
          <Ionicons name="chevron-forward" size={16} color={colors.textTertiary} />
        </Pressable>
        <Pressable onPress={confirmSignOut} style={styles.linkRow}>
          <Ionicons name="log-out-outline" size={18} color={colors.danger} />
          <Text style={[styles.linkRowText, { color: colors.danger }]}>Sign out</Text>
        </Pressable>
      </Section>

      <Text style={[styles.version, { color: colors.textTertiary }]}>
        SpotApply {Constants.expoConfig?.version ?? ""} · The final Submit is always yours.
      </Text>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  center: {
    alignItems: "center",
    justifyContent: "center",
  },
  content: {
    padding: 16,
    gap: 14,
    paddingBottom: 40,
  },
  identity: {
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    paddingVertical: 8,
  },
  avatar: {
    width: 56,
    height: 56,
    borderRadius: 28,
    alignItems: "center",
    justifyContent: "center",
  },
  avatarText: {
    fontSize: 20,
    fontWeight: "800",
  },
  identityText: {
    flex: 1,
    gap: 2,
  },
  name: {
    fontSize: 19,
    fontWeight: "800",
  },
  email: {
    fontSize: 13,
  },
  resumeRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  resumeText: {
    fontSize: 14,
    fontWeight: "600",
    flex: 1,
  },
  webLink: {
    fontSize: 13,
    fontWeight: "700",
  },
  meter: {
    gap: 6,
  },
  meterHeader: {
    flexDirection: "row",
    justifyContent: "space-between",
  },
  meterLabel: {
    fontSize: 13,
    fontWeight: "600",
  },
  meterValue: {
    fontSize: 13,
    fontWeight: "800",
    fontVariant: ["tabular-nums"],
  },
  meterTrack: {
    height: 6,
    borderRadius: 3,
    overflow: "hidden",
  },
  meterFill: {
    height: 6,
    borderRadius: 3,
  },
  trial: {
    fontSize: 13,
    fontWeight: "700",
  },
  rolesWrap: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
  },
  roleChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    borderRadius: 999,
    paddingHorizontal: 12,
    paddingVertical: 7,
  },
  roleChipText: {
    fontSize: 13,
    fontWeight: "700",
  },
  roleInputRow: {
    flexDirection: "row",
    gap: 8,
  },
  roleInput: {
    flex: 1,
    borderWidth: 1,
    borderRadius: 12,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 14,
  },
  roleAdd: {
    width: 44,
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
  },
  saveButton: {
    borderRadius: 12,
    paddingVertical: 12,
    alignItems: "center",
  },
  saveButtonText: {
    color: "#FFFFFF",
    fontSize: 15,
    fontWeight: "700",
  },
  linkRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingVertical: 10,
  },
  linkRowText: {
    fontSize: 15,
    fontWeight: "600",
    flex: 1,
  },
  version: {
    textAlign: "center",
    fontSize: 12,
    marginTop: 8,
  },
});
