import { StyleSheet, Text, View } from "react-native";

import { type ThemeColors } from "@/constants/theme";
import { useTheme } from "@/hooks/use-theme";
import type { ApplicationStatus } from "@/lib/types";

export const STATUS_LABELS: Record<ApplicationStatus, string> = {
  discovered: "Discovered",
  matched: "Matched",
  shortlisted: "Shortlisted",
  tailored: "Tailored",
  autofilled: "Autofilled",
  awaiting_user: "Awaiting you",
  ready_to_submit: "Ready to submit",
  submitted: "Submitted",
  rejected: "Rejected",
  interviewing: "Interviewing",
  offer: "Offer",
  accepted: "Accepted",
  skipped: "Skipped",
  error: "Error",
};

function statusColors(colors: ThemeColors, status: ApplicationStatus): { fg: string; bg: string } {
  switch (status) {
    case "shortlisted":
    case "matched":
      return { fg: colors.info, bg: colors.infoSoft };
    case "tailored":
    case "autofilled":
    case "awaiting_user":
    case "ready_to_submit":
      return { fg: colors.warning, bg: colors.warningSoft };
    case "submitted":
    case "interviewing":
    case "offer":
    case "accepted":
      return { fg: colors.accentText, bg: colors.accentSoft };
    case "rejected":
    case "error":
      return { fg: colors.danger, bg: colors.dangerSoft };
    default:
      return { fg: colors.textSecondary, bg: colors.neutralSoft };
  }
}

export function StatusPill({ status }: { status: ApplicationStatus }) {
  const { colors } = useTheme();
  const { fg, bg } = statusColors(colors, status);
  return (
    <View style={[styles.pill, { backgroundColor: bg }]}>
      <Text style={[styles.label, { color: fg }]}>{STATUS_LABELS[status] ?? status}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  pill: {
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 999,
    alignSelf: "flex-start",
  },
  label: {
    fontSize: 11,
    fontWeight: "700",
  },
});
