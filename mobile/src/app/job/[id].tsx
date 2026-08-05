import { Ionicons } from "@expo/vector-icons";
import { useLocalSearchParams } from "expo-router";
import * as WebBrowser from "expo-web-browser";
import { useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { EmptyState } from "@/components/empty-state";
import { ScoreBadge } from "@/components/score-badge";
import { Section } from "@/components/section";
import { StatusPill } from "@/components/status-pill";
import { useTheme } from "@/hooks/use-theme";
import { verifyJob } from "@/lib/api";
import { API_URL } from "@/lib/config";
import { getCachedJob, updateCachedJob } from "@/lib/job-cache";
import { formatDate, percent } from "@/lib/format";
import type { JobSummary } from "@/lib/types";

function Bar({ value, color, track }: { value: number; color: string; track: string }) {
  return (
    <View style={[styles.barTrack, { backgroundColor: track }]}>
      <View
        style={[
          styles.barFill,
          { backgroundColor: color, width: `${Math.max(2, Math.min(100, value))}%` },
        ]}
      />
    </View>
  );
}

export default function JobDetailScreen() {
  const { colors } = useTheme();
  const { id } = useLocalSearchParams<{ id: string }>();
  const [job, setJob] = useState<JobSummary | undefined>(() => getCachedJob(Number(id)));
  const [verifying, setVerifying] = useState(false);

  if (!job) {
    return (
      <View style={[styles.flex, { backgroundColor: colors.background }]}>
        <EmptyState
          icon="alert-circle-outline"
          title="Job not loaded"
          message="Open this job from your feed — details are loaded from the jobs list."
        />
      </View>
    );
  }

  const openPosting = () => WebBrowser.openBrowserAsync(job.url);
  const openDashboard = () => WebBrowser.openBrowserAsync(`${API_URL}/dashboard`);

  const checkStillOpen = async () => {
    setVerifying(true);
    try {
      const result = await verifyJob(job.id);
      if (result.active) {
        Alert.alert("Still open", "This posting is live — go get it.");
      } else {
        const updated = updateCachedJob(job.id, {
          is_closed: true,
          closed_reason: result.closed_reason ?? "Closed",
        });
        if (updated) setJob(updated);
        Alert.alert("Posting closed", result.closed_reason ?? "This job is no longer accepting applications.");
      }
    } catch (e) {
      Alert.alert("Couldn't verify", e instanceof Error ? e.message : "Try again later.");
    } finally {
      setVerifying(false);
    }
  };

  const hireProbPct = job.hire_probability !== null ? Math.round(job.hire_probability * 100) : null;

  return (
    <ScrollView
      style={[styles.flex, { backgroundColor: colors.background }]}
      contentContainerStyle={styles.content}>
      {job.is_closed ? (
        <View style={[styles.closedBanner, { backgroundColor: colors.dangerSoft }]}>
          <Ionicons name="close-circle" size={16} color={colors.danger} />
          <Text style={[styles.closedText, { color: colors.danger }]}>
            {job.closed_reason || "This posting is closed."}
          </Text>
        </View>
      ) : null}

      <View style={styles.header}>
        <View style={styles.headerText}>
          <Text style={[styles.company, { color: colors.textSecondary }]}>{job.company}</Text>
          <Text style={[styles.title, { color: colors.text }]}>{job.title}</Text>
          <View style={styles.metaRow}>
            {job.location ? (
              <Text style={[styles.meta, { color: colors.textTertiary }]}>{job.location}</Text>
            ) : null}
            {job.remote ? (
              <Text style={[styles.meta, { color: colors.textTertiary }]}>· Remote</Text>
            ) : null}
          </View>
          <Text style={[styles.meta, { color: colors.textTertiary }]}>
            Posted {formatDate(job.posted)} · via {job.source}
          </Text>
        </View>
        <ScoreBadge score={job.rerank} size="large" />
      </View>

      <Section title="Match breakdown">
        <View style={styles.scoreRow}>
          <Text style={[styles.scoreLabel, { color: colors.textSecondary }]}>Priority</Text>
          <Text style={[styles.scoreValue, { color: colors.text }]}>
            {job.blended !== null ? Math.round(job.blended) : "—"}
          </Text>
        </View>
        {job.blended !== null ? (
          <Bar value={job.blended} color={colors.accent} track={colors.neutralSoft} />
        ) : null}

        <View style={styles.scoreRow}>
          <Text style={[styles.scoreLabel, { color: colors.textSecondary }]}>Hire probability</Text>
          <Text style={[styles.scoreValue, { color: colors.text }]}>
            {hireProbPct !== null ? `${hireProbPct}%` : "—"}
          </Text>
        </View>
        {hireProbPct !== null ? (
          <Bar value={hireProbPct} color={colors.info} track={colors.neutralSoft} />
        ) : null}

        {job.similarity !== null ? (
          <Text style={[styles.similarity, { color: colors.textTertiary }]}>
            Résumé similarity {percent(job.similarity)}
          </Text>
        ) : null}
      </Section>

      {job.reason ? (
        <Section title="Why this match">
          <Text style={[styles.reason, { color: colors.text }]}>{job.reason}</Text>
        </Section>
      ) : null}

      <Section title="Application">
        {job.application ? (
          <View style={styles.appRow}>
            <StatusPill status={job.application.status} />
            <Text style={[styles.meta, { color: colors.textTertiary }]}>
              Updated {formatDate(job.application.updated_at)}
            </Text>
          </View>
        ) : (
          <Text style={[styles.meta, { color: colors.textSecondary }]}>
            Not in your pipeline yet. Tailoring and autofill run from the web dashboard — the final
            Submit is always yours.
          </Text>
        )}
      </Section>

      <View style={styles.actions}>
        <Pressable
          onPress={openPosting}
          style={({ pressed }) => [
            styles.primaryButton,
            { backgroundColor: colors.accent, opacity: pressed ? 0.85 : 1 },
          ]}>
          <Ionicons name="open-outline" size={18} color="#FFFFFF" />
          <Text style={styles.primaryButtonText}>Open posting</Text>
        </Pressable>

        <Pressable
          onPress={checkStillOpen}
          disabled={verifying || job.is_closed}
          style={({ pressed }) => [
            styles.secondaryButton,
            {
              borderColor: colors.border,
              backgroundColor: colors.card,
              opacity: pressed || verifying || job.is_closed ? 0.6 : 1,
            },
          ]}>
          {verifying ? (
            <ActivityIndicator size="small" color={colors.accent} />
          ) : (
            <Ionicons name="shield-checkmark-outline" size={18} color={colors.text} />
          )}
          <Text style={[styles.secondaryButtonText, { color: colors.text }]}>
            Check if still open
          </Text>
        </Pressable>

        <Pressable onPress={openDashboard} hitSlop={8} style={styles.dashboardLink}>
          <Text style={[styles.dashboardLinkText, { color: colors.textSecondary }]}>
            Tailor & apply on the web dashboard →
          </Text>
        </Pressable>
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  content: {
    padding: 16,
    gap: 14,
    paddingBottom: 40,
  },
  closedBanner: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    borderRadius: 12,
    padding: 12,
  },
  closedText: {
    fontSize: 13,
    fontWeight: "700",
    flex: 1,
  },
  header: {
    flexDirection: "row",
    gap: 14,
    alignItems: "flex-start",
  },
  headerText: {
    flex: 1,
    gap: 4,
  },
  company: {
    fontSize: 14,
    fontWeight: "600",
  },
  title: {
    fontSize: 21,
    fontWeight: "800",
    lineHeight: 27,
  },
  metaRow: {
    flexDirection: "row",
    gap: 4,
    flexWrap: "wrap",
  },
  meta: {
    fontSize: 13,
    lineHeight: 19,
  },
  scoreRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
  },
  scoreLabel: {
    fontSize: 14,
    fontWeight: "600",
  },
  scoreValue: {
    fontSize: 15,
    fontWeight: "800",
    fontVariant: ["tabular-nums"],
  },
  barTrack: {
    height: 6,
    borderRadius: 3,
    overflow: "hidden",
  },
  barFill: {
    height: 6,
    borderRadius: 3,
  },
  similarity: {
    fontSize: 12,
    marginTop: 2,
  },
  reason: {
    fontSize: 15,
    lineHeight: 22,
  },
  appRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
  },
  actions: {
    gap: 10,
    marginTop: 4,
  },
  primaryButton: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    borderRadius: 12,
    paddingVertical: 14,
  },
  primaryButtonText: {
    color: "#FFFFFF",
    fontSize: 16,
    fontWeight: "700",
  },
  secondaryButton: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    borderRadius: 12,
    paddingVertical: 13,
    borderWidth: 1,
  },
  secondaryButtonText: {
    fontSize: 15,
    fontWeight: "700",
  },
  dashboardLink: {
    alignItems: "center",
    paddingVertical: 6,
  },
  dashboardLinkText: {
    fontSize: 13,
    fontWeight: "600",
  },
});
