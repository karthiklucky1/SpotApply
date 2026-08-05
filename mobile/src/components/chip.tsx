import { Pressable, StyleSheet, Text } from "react-native";

import { useTheme } from "@/hooks/use-theme";

interface ChipProps {
  label: string;
  selected?: boolean;
  onPress?: () => void;
}

export function Chip({ label, selected = false, onPress }: ChipProps) {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [
        styles.chip,
        {
          backgroundColor: selected ? colors.accentSoft : colors.chipBackground,
          borderColor: selected ? colors.accent : "transparent",
          opacity: pressed ? 0.7 : 1,
        },
      ]}>
      <Text
        style={[
          styles.label,
          { color: selected ? colors.accentText : colors.textSecondary },
        ]}>
        {label}
      </Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  chip: {
    paddingHorizontal: 14,
    paddingVertical: 7,
    borderRadius: 999,
    borderWidth: 1,
  },
  label: {
    fontSize: 13,
    fontWeight: "600",
  },
});
