import { StyleSheet, Text, View } from "react-native";

import { useTheme } from "@/hooks/use-theme";

/** Wordmark + tagline shown on the auth screens. */
export function BrandHeader() {
  const { colors } = useTheme();
  return (
    <View style={styles.wrap}>
      <Text style={styles.wordmark}>
        <Text style={{ color: colors.accent }}>Spot</Text>
        <Text style={{ color: colors.text }}>Apply</Text>
      </Text>
      <Text style={[styles.tagline, { color: colors.textSecondary }]}>
        Your AI job-application copilot
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    alignItems: "center",
    gap: 6,
  },
  wordmark: {
    fontSize: 34,
    fontWeight: "800",
    letterSpacing: -0.5,
  },
  tagline: {
    fontSize: 15,
  },
});
