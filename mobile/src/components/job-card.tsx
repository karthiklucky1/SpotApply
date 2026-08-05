import { Ionicons } from "@expo/vector-icons";
import { StyleSheet, Text, View, Pressable } from "react-native";

import { ScoreBadge } from "@/components/score-badge";
import { StatusPill } from "@/components/status-pill";
import { useTheme } from "@/hooks/use-theme";
import { timeAgo } from "@/lib/format";
import type { JobSummary } from "@/lib/types";

export function JobCard({ job, onPress }: { job: JobSummary; onPress: () => void }) {
  const { colors } = useTheme();
  const posted = timeAgo(job.posted);
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [
        styles.card,
        {
          backgroundColor: colors.card,
          borderColor: colors.border,
          opacity: pressed ? 0.85 : 1,
        },
      ]}>
      <View style={styles.topRow}>
        <View style={styles.titleBlock}>
          <Text style={[styles.company, { color: colors.textSecondary }]} numberOfLines={1}>
            {job.company}
          </Text>
          <Text style={[styles.title, { color: colors.text }]} numberOfLines={2}>
            {job.title}
          </Text>
        </View>
        <ScoreBadge score={job.rerank} />
      </View>

      <View style={styles.metaRow}>
        {job.is_new ? (
          <View style={[styles.newDot, { backgroundColor: colors.accent }]} />
        ) : null}
        {job.is_new ? (
          <Text style={[styles.meta, { color: colors.accentText, fontWeight: "700" }]}>New</Text>
        ) : null}
        {job.location ? (
          <>
            <Ionicons name="location-outline" size={12} color={colors.textTertiary} />
            <Text style={[styles.meta, { color: colors.textTertiary }]} numberOfLines={1}>
              {job.location}
            </Text>
          </>
        ) : null}
        {job.remote ? (
          <Text style={[styles.meta, { color: colors.textTertiary }]}>· Remote</Text>
        ) : null}
        {posted ? (
          <Text style={[styles.meta, { color: colors.textTertiary }]}>· {posted}</Text>
        ) : null}
      </View>

      {job.application ? (
        <View style={styles.statusRow}>
          <StatusPill status={job.application.status} />
        </View>
      ) : null}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  card: {
    borderWidth: 1,
    borderRadius: 16,
    padding: 14,
    gap: 8,
  },
  topRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 12,
  },
  titleBlock: {
    flex: 1,
    gap: 2,
  },
  company: {
    fontSize: 13,
    fontWeight: "600",
  },
  title: {
    fontSize: 16,
    fontWeight: "700",
    lineHeight: 21,
  },
  metaRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    flexWrap: "wrap",
  },
  newDot: {
    width: 6,
    height: 6,
    borderRadius: 3,
  },
  meta: {
    fontSize: 12,
    flexShrink: 1,
  },
  statusRow: {
    flexDirection: "row",
  },
});
