import { Ionicons } from "@expo/vector-icons";
import * as Haptics from "expo-haptics";
import { router } from "expo-router";
import * as WebBrowser from "expo-web-browser";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ActivityIndicator,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { Chip } from "@/components/chip";
import { EmptyState } from "@/components/empty-state";
import { JobCard } from "@/components/job-card";
import { useTheme } from "@/hooks/use-theme";
import { getJobs, getResumeStatus } from "@/lib/api";
import { API_URL } from "@/lib/config";
import { rememberJobs } from "@/lib/job-cache";
import type { JobsQuery, JobSummary } from "@/lib/types";

type Filter = "top" | "fresh" | "all";

const FILTERS: { key: Filter; label: string }[] = [
  { key: "top", label: "Top matches" },
  { key: "fresh", label: "Fresh" },
  { key: "all", label: "All jobs" },
];

const QUERY_FOR: Record<Filter, JobsQuery> = {
  top: { min_score: 60, roles_only: "1", hide_aggregators: "1" },
  fresh: { sort: "fresh", max_age_days: 7, roles_only: "1", hide_aggregators: "1" },
  all: {},
};

const PAGE_SIZE = 30;

export default function JobsScreen() {
  const { colors } = useTheme();
  const [filter, setFilter] = useState<Filter>("top");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasResume, setHasResume] = useState<boolean | null>(null);
  const requestId = useRef(0);

  // Debounce the search box so we don't hit the API on every keystroke.
  useEffect(() => {
    const t = setTimeout(() => setSearch(searchInput.trim()), 400);
    return () => clearTimeout(t);
  }, [searchInput]);

  useEffect(() => {
    getResumeStatus()
      .then((r) => setHasResume(r.has_resume))
      .catch(() => setHasResume(null));
  }, []);

  const loadPage = useCallback(
    async (target: number, mode: "reset" | "refresh" | "append") => {
      const id = ++requestId.current;
      if (mode === "reset") setLoading(true);
      if (mode === "refresh") setRefreshing(true);
      if (mode === "append") setLoadingMore(true);
      try {
        const res = await getJobs({
          ...QUERY_FOR[filter],
          search: search || undefined,
          page: target,
          limit: PAGE_SIZE,
        });
        if (id !== requestId.current) return;
        rememberJobs(res.jobs);
        setJobs((prev) => (mode === "append" ? [...prev, ...res.jobs] : res.jobs));
        setTotal(res.total);
        setPage(res.page);
        setPages(res.pages);
        setError(null);
      } catch (e) {
        if (id !== requestId.current) return;
        setError(e instanceof Error ? e.message : "Couldn't load jobs");
      } finally {
        if (id === requestId.current) {
          setLoading(false);
          setRefreshing(false);
          setLoadingMore(false);
        }
      }
    },
    [filter, search],
  );

  useEffect(() => {
    loadPage(1, "reset");
  }, [loadPage]);

  const onEndReached = () => {
    if (!loading && !loadingMore && !refreshing && page < pages) {
      loadPage(page + 1, "append");
    }
  };

  const selectFilter = (next: Filter) => {
    if (next !== filter) {
      Haptics.selectionAsync();
      setFilter(next);
    }
  };

  const openJob = (job: JobSummary) => {
    router.push(`/job/${job.id}`);
  };

  const emptyState = () => {
    if (loading) return null;
    if (error) {
      return <EmptyState icon="cloud-offline-outline" title="Couldn't load jobs" message={error} />;
    }
    if (hasResume === false) {
      return (
        <View>
          <EmptyState
            icon="document-text-outline"
            title="Upload your résumé to start matching"
            message="SpotApply scores every job against your résumé. Upload it on the web dashboard and your feed will fill in automatically."
          />
          <Pressable
            onPress={() => WebBrowser.openBrowserAsync(`${API_URL}/dashboard`)}
            style={[styles.emptyAction, { backgroundColor: colors.accent }]}>
            <Text style={styles.emptyActionText}>Open web dashboard</Text>
          </Pressable>
        </View>
      );
    }
    return (
      <EmptyState
        icon="sparkles-outline"
        title="No matches here yet"
        message={
          filter === "top"
            ? "SpotApply is still scanning and scoring roles for you — check back soon, or try the All jobs filter."
            : "Nothing matches this filter right now. New postings are scanned around the clock."
        }
      />
    );
  };

  return (
    <View style={[styles.flex, { backgroundColor: colors.background }]}>
      <View style={styles.controls}>
        <View
          style={[
            styles.searchBox,
            { backgroundColor: colors.inputBackground, borderColor: colors.border },
          ]}>
          <Ionicons name="search-outline" size={18} color={colors.textTertiary} />
          <TextInput
            value={searchInput}
            onChangeText={setSearchInput}
            placeholder="Search title, company, location"
            placeholderTextColor={colors.textTertiary}
            style={[styles.searchInput, { color: colors.text }]}
            autoCorrect={false}
            returnKeyType="search"
          />
          {searchInput ? (
            <Pressable onPress={() => setSearchInput("")} hitSlop={8}>
              <Ionicons name="close-circle" size={18} color={colors.textTertiary} />
            </Pressable>
          ) : null}
        </View>

        <View style={styles.chipRow}>
          {FILTERS.map((f) => (
            <Chip
              key={f.key}
              label={f.label}
              selected={filter === f.key}
              onPress={() => selectFilter(f.key)}
            />
          ))}
        </View>

        {!loading && !error ? (
          <Text style={[styles.countLine, { color: colors.textTertiary }]}>
            {total.toLocaleString()} {total === 1 ? "match" : "matches"}
          </Text>
        ) : null}
      </View>

      {loading ? (
        <View style={styles.loadingWrap}>
          <ActivityIndicator color={colors.accent} size="large" />
        </View>
      ) : (
        <FlatList
          data={jobs}
          keyExtractor={(item) => String(item.id)}
          renderItem={({ item }) => <JobCard job={item} onPress={() => openJob(item)} />}
          contentContainerStyle={styles.listContent}
          ItemSeparatorComponent={() => <View style={styles.separator} />}
          ListEmptyComponent={emptyState()}
          ListFooterComponent={
            loadingMore ? (
              <ActivityIndicator color={colors.accent} style={styles.footerSpinner} />
            ) : null
          }
          onEndReached={onEndReached}
          onEndReachedThreshold={0.4}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={() => loadPage(1, "refresh")}
              tintColor={colors.accent}
            />
          }
        />
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  controls: {
    paddingHorizontal: 16,
    paddingTop: 12,
    gap: 10,
  },
  searchBox: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    borderWidth: 1,
    borderRadius: 12,
    paddingHorizontal: 12,
    paddingVertical: 9,
  },
  searchInput: {
    flex: 1,
    fontSize: 15,
    padding: 0,
  },
  chipRow: {
    flexDirection: "row",
    gap: 8,
  },
  countLine: {
    fontSize: 12,
    fontWeight: "600",
  },
  loadingWrap: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
  },
  listContent: {
    padding: 16,
    paddingBottom: 32,
  },
  separator: {
    height: 10,
  },
  footerSpinner: {
    marginVertical: 16,
  },
  emptyAction: {
    alignSelf: "center",
    borderRadius: 12,
    paddingHorizontal: 20,
    paddingVertical: 12,
  },
  emptyActionText: {
    color: "#FFFFFF",
    fontSize: 15,
    fontWeight: "700",
  },
});
