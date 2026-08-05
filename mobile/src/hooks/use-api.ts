import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Minimal data-loading hook: fires the fetcher on mount (and when `deps`
 * change), exposes pull-to-refresh via `refresh`. Stale responses are ignored
 * so a slow earlier request can never overwrite a newer one.
 */
export function useApi<T>(fetcher: () => Promise<T>, deps: readonly unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const requestId = useRef(0);

  const load = useCallback(
    async (mode: "initial" | "refresh" = "initial") => {
      const id = ++requestId.current;
      if (mode === "refresh") setRefreshing(true);
      else setLoading(true);
      try {
        const result = await fetcher();
        if (id !== requestId.current) return;
        setData(result);
        setError(null);
      } catch (e) {
        if (id !== requestId.current) return;
        setError(e instanceof Error ? e.message : "Something went wrong");
      } finally {
        if (id === requestId.current) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    deps,
  );

  useEffect(() => {
    load();
  }, [load]);

  const refresh = useCallback(() => load("refresh"), [load]);

  return { data, setData, error, loading, refreshing, refresh, reload: load };
}
