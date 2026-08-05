import { StyleSheet, Text, View, type ViewStyle } from "react-native";

import { useTheme } from "@/hooks/use-theme";

interface SectionProps {
  title?: string;
  children: React.ReactNode;
  style?: ViewStyle;
}

/** Card container used across detail/profile screens. */
export function Section({ title, children, style }: SectionProps) {
  const { colors } = useTheme();
  return (
    <View
      style={[
        styles.card,
        { backgroundColor: colors.card, borderColor: colors.border },
        style,
      ]}>
      {title ? (
        <Text style={[styles.title, { color: colors.textSecondary }]}>{title}</Text>
      ) : null}
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    borderWidth: 1,
    borderRadius: 16,
    padding: 16,
    gap: 10,
  },
  title: {
    fontSize: 12,
    fontWeight: "700",
    textTransform: "uppercase",
    letterSpacing: 0.6,
  },
});
