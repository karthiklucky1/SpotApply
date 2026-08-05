import { StyleSheet, Text, View } from "react-native";

import { scoreColors } from "@/constants/theme";
import { useTheme } from "@/hooks/use-theme";

export function ScoreBadge({ score, size = "small" }: { score: number | null; size?: "small" | "large" }) {
  const { colors } = useTheme();
  const { fg, bg } = scoreColors(colors, score);
  const large = size === "large";
  return (
    <View style={[styles.badge, large && styles.badgeLarge, { backgroundColor: bg }]}>
      <Text style={[styles.value, large && styles.valueLarge, { color: fg }]}>
        {score === null || score === undefined ? "—" : Math.round(score)}
      </Text>
      {large ? <Text style={[styles.caption, { color: fg }]}>fit score</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    minWidth: 40,
    paddingHorizontal: 8,
    paddingVertical: 4,
    borderRadius: 10,
    alignItems: "center",
    justifyContent: "center",
  },
  badgeLarge: {
    minWidth: 76,
    paddingVertical: 10,
    borderRadius: 16,
  },
  value: {
    fontSize: 15,
    fontWeight: "700",
    fontVariant: ["tabular-nums"],
  },
  valueLarge: {
    fontSize: 28,
  },
  caption: {
    fontSize: 11,
    fontWeight: "600",
    opacity: 0.85,
  },
});
