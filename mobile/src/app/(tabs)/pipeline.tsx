import { router } from "expo-router";
import { useState } from "react";
import {
  ActivityIndicator,
  FlatList,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { Chip } from "@/components/chip";
import { EmptyState } from "@/components/empty-state";
import { JobCard } from "@/components/job-card";
import { StatTile } from "@/components/stat-tile";
import { STATUS_LABELS } from "@/components/status-pill";
import { useTheme } from "@/hooks/use-theme";
import { useApi } from "@/hooks/use-api";
import { getFunnel, getJobs } from "@/lib/api";
import { rememberJobs } from "@/lib/job-cache";
import type { ApplicationStatus, JobSummary } from "@/lib/types";

const STAGES: ApplicationStatus[] = [
  "shortlisted",
  "submitted",
  "interviewing",
  "offer",
  "rejected",
];

export default function PipelineScreen() {
  const { colors } = useTheme();
  const [stage, setStage] = useState<ApplicationStatus>("shortlisted");

  const funnel = useApi(() => getFunnel(), []);
  const jobsQuery = useApi(async () => {
    const res = await getJobs({ status: stage, limit: 50 });
    rememberJobs(res.jobs);
    return res;
  }, [stage]);

  const refreshing = funnel.refreshing || jobsQuery.refreshing;
  const refreshAll = () => {
    funnel.refresh();
    jobsQuery.refresh();
  };

  const jobs: JobSummary[] = jobsQuery.data?.jobs ?? [];
  const f = funnel.data;

  const header = (
    <View style={styles.header}>
      {f ? (
        <View style={styles.statsGrid}>
          <StatTile value={f.applied} label="Applied" />
          <StatTile value={f.interviewing} label="Interviewing" tone="accent" />
          <StatTile value={f.offers} label="Offers" tone="accent" />
          <StatTile value={`${f.response_rate}%`} label="Response rate" />
          <StatTile value={f.applied_to_interview + "%"} label="→ Interview" />
          <StatTile
            value={f.ghosted + f.presumed_ghosted}
            label="Ghosted"
            tone={f.ghosted + f.presumed_ghosted > 0 ? "warning" : "default"}
          />
        </View>
      ) : null}

      <FlatList
        horizontal
        showsHorizontalScrollIndicator={false}
        data={STAGES}
        keyExtractor={(s) => s}
        contentContainerStyle={styles.chipRow}
        renderItem={({ item }) => (
          <Chip
            label={STATUS_LABELS[item]}
            selected={stage === item}
            onPress={() => setStage(item)}
          />
        )}
      />

      {!jobsQuery.loading ? (
        <Text style={[styles.countLine, { color: colors.textTertiary }]}>
          {jobsQuery.data?.total ?? 0} in {STATUS_LABELS[stage].toLowerCase()}
        </Text>
      ) : null}
    </View>
  );

  return (
    <View style={[styles.flex, { backgroundColor: colors.background }]}>
      <FlatList
        data={jobsQuery.loading ? [] : jobs}
        keyExtractor={(item) => String(item.id)}
        renderItem={({ item }) => (
          <JobCard job={item} onPress={() => router.push(`/job/${item.id}`)} />
        )}
        ListHeaderComponent={header}
        ListEmptyComponent={
          jobsQuery.loading ? (
            <ActivityIndicator color={colors.accent} style={styles.spinner} size="large" />
          ) : jobsQuery.error ? (
            <EmptyState
              icon="cloud-offline-outline"
              title="Couldn't load pipeline"
              message={jobsQuery.error}
            />
          ) : (
            <EmptyState
              icon="albums-outline"
              title={`Nothing ${STATUS_LABELS[stage].toLowerCase()} yet`}
              message="Jobs move through your pipeline as SpotApply shortlists them and you apply."
            />
          )
        }
        ItemSeparatorComponent={() => <View style={styles.separator} />}
        contentContainerStyle={styles.listContent}
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={refreshAll} tintColor={colors.accent} />
        }
      />
    </View>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  header: {
    gap: 12,
    paddingBottom: 12,
  },
  statsGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: 8,
  },
  chipRow: {
    gap: 8,
  },
  countLine: {
    fontSize: 12,
    fontWeight: "600",
  },
  spinner: {
    marginTop: 48,
  },
  listContent: {
    padding: 16,
    paddingBottom: 32,
  },
  separator: {
    height: 10,
  },
});
