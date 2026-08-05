import { StyleSheet, Text, View } from "react-native";

import { useTheme } from "@/hooks/use-theme";

interface StatTileProps {
  value: string | number;
  label: string;
  tone?: "default" | "accent" | "warning" | "danger";
}

export function StatTile({ value, label, tone = "default" }: StatTileProps) {
  const { colors } = useTheme();
  const valueColor =
    tone === "accent"
      ? colors.accentText
      : tone === "warning"
        ? colors.warning
        : tone === "danger"
          ? colors.danger
          : colors.text;
  return (
    <View style={[styles.tile, { backgroundColor: colors.card, borderColor: colors.border }]}>
      <Text style={[styles.value, { color: valueColor }]}>{value}</Text>
      <Text style={[styles.label, { color: colors.textSecondary }]} numberOfLines={1}>
        {label}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  tile: {
    flex: 1,
    minWidth: "30%",
    borderWidth: 1,
    borderRadius: 14,
    paddingVertical: 14,
    paddingHorizontal: 10,
    alignItems: "center",
    gap: 2,
  },
  value: {
    fontSize: 22,
    fontWeight: "800",
    fontVariant: ["tabular-nums"],
  },
  label: {
    fontSize: 12,
    fontWeight: "600",
  },
});
