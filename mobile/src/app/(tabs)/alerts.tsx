import { Ionicons } from "@expo/vector-icons";
import * as WebBrowser from "expo-web-browser";
import {
  ActivityIndicator,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { EmptyState } from "@/components/empty-state";
import { useTheme } from "@/hooks/use-theme";
import { useApi } from "@/hooks/use-api";
import {
  getNotifications,
  markAllNotificationsRead,
  markNotificationRead,
} from "@/lib/api";
import { API_URL } from "@/lib/config";
import { timeAgo } from "@/lib/format";
import type { NotificationItem } from "@/lib/types";

function iconFor(type: string | null): keyof typeof Ionicons.glyphMap {
  switch (type) {
    case "fresh_match":
    case "new_matches":
      return "sparkles-outline";
    case "tailor_failed":
      return "alert-circle-outline";
    case "tailor_done":
      return "document-text-outline";
    default:
      return "notifications-outline";
  }
}

export default function AlertsScreen() {
  const { colors } = useTheme();
  const query = useApi(async () => (await getNotifications()).notifications, []);
  const items = query.data ?? [];
  const unread = items.filter((n) => !n.read).length;

  const openItem = async (item: NotificationItem) => {
    if (!item.read) {
      // Optimistic: flip locally, then tell the server.
      query.setData(items.map((n) => (n.id === item.id ? { ...n, read: true } : n)));
      markNotificationRead(item.id).catch(() => {});
    }
    if (item.link) {
      const url = item.link.startsWith("http") ? item.link : `${API_URL}${item.link}`;
      WebBrowser.openBrowserAsync(url);
    }
  };

  const markAll = async () => {
    query.setData(items.map((n) => ({ ...n, read: true })));
    markAllNotificationsRead().catch(() => {});
  };

  return (
    <View style={[styles.flex, { backgroundColor: colors.background }]}>
      <View style={styles.topRow}>
        <Text style={[styles.unreadLine, { color: colors.textTertiary }]}>
          {unread ? `${unread} unread` : "All caught up"}
        </Text>
        {unread ? (
          <Pressable onPress={markAll} hitSlop={8}>
            <Text style={[styles.markAll, { color: colors.accentText }]}>Mark all read</Text>
          </Pressable>
        ) : null}
      </View>

      <FlatList
        data={query.loading ? [] : items}
        keyExtractor={(item) => String(item.id)}
        renderItem={({ item }) => (
          <Pressable
            onPress={() => openItem(item)}
            style={({ pressed }) => [
              styles.item,
              {
                backgroundColor: colors.card,
                borderColor: item.read ? colors.border : colors.accent,
                opacity: pressed ? 0.85 : 1,
              },
            ]}>
            <View
              style={[
                styles.iconWrap,
                { backgroundColor: item.read ? colors.neutralSoft : colors.accentSoft },
              ]}>
              <Ionicons
                name={iconFor(item.type)}
                size={18}
                color={item.read ? colors.textTertiary : colors.accentText}
              />
            </View>
            <View style={styles.itemBody}>
              <Text
                style={[
                  styles.itemTitle,
                  { color: colors.text, fontWeight: item.read ? "600" : "800" },
                ]}
                numberOfLines={2}>
                {item.title}
              </Text>
              <Text style={[styles.itemMessage, { color: colors.textSecondary }]} numberOfLines={3}>
                {item.message}
              </Text>
              <Text style={[styles.itemTime, { color: colors.textTertiary }]}>
                {timeAgo(item.created_at)}
              </Text>
            </View>
            {!item.read ? <View style={[styles.dot, { backgroundColor: colors.accent }]} /> : null}
          </Pressable>
        )}
        ListEmptyComponent={
          query.loading ? (
            <ActivityIndicator color={colors.accent} style={styles.spinner} size="large" />
          ) : query.error ? (
            <EmptyState
              icon="cloud-offline-outline"
              title="Couldn't load alerts"
              message={query.error}
            />
          ) : (
            <EmptyState
              icon="notifications-off-outline"
              title="No alerts yet"
              message="Fresh high-fit matches and pipeline updates will land here."
            />
          )
        }
        ItemSeparatorComponent={() => <View style={styles.separator} />}
        contentContainerStyle={styles.listContent}
        refreshControl={
          <RefreshControl
            refreshing={query.refreshing}
            onRefresh={query.refresh}
            tintColor={colors.accent}
          />
        }
      />
    </View>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  topRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    paddingHorizontal: 16,
    paddingTop: 12,
  },
  unreadLine: {
    fontSize: 12,
    fontWeight: "600",
  },
  markAll: {
    fontSize: 13,
    fontWeight: "700",
  },
  item: {
    flexDirection: "row",
    gap: 12,
    borderWidth: 1,
    borderRadius: 14,
    padding: 12,
  },
  iconWrap: {
    width: 36,
    height: 36,
    borderRadius: 18,
    alignItems: "center",
    justifyContent: "center",
  },
  itemBody: {
    flex: 1,
    gap: 2,
  },
  itemTitle: {
    fontSize: 14,
  },
  itemMessage: {
    fontSize: 13,
    lineHeight: 18,
  },
  itemTime: {
    fontSize: 11,
    marginTop: 2,
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    marginTop: 4,
  },
  spinner: {
    marginTop: 48,
  },
  listContent: {
    padding: 16,
    paddingBottom: 32,
  },
  separator: {
    height: 8,
  },
});
