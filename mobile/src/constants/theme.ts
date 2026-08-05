/**
 * SpotApply mobile theme — mirrors the web brand (emerald primary, indigo
 * secondary, deep-navy dark surfaces). Light and dark palettes share keys so
 * components stay scheme-agnostic via useTheme().
 */

import '@/global.css';

import { Platform } from 'react-native';

export const Colors = {
  light: {
    background: '#F8FAFC',
    card: '#FFFFFF',
    border: '#E2E8F0',
    inputBackground: '#FFFFFF',
    chipBackground: '#EDF2F7',
    text: '#0F172A',
    textSecondary: '#475569',
    textTertiary: '#94A3B8',
    accent: '#059669',
    accentSoft: '#D1FAE5',
    accentText: '#065F46',
    info: '#4F46E5',
    infoSoft: '#E0E7FF',
    warning: '#B45309',
    warningSoft: '#FEF3C7',
    danger: '#DC2626',
    dangerSoft: '#FEE2E2',
    neutralSoft: '#F1F5F9',
  },
  dark: {
    background: '#0B1120',
    card: '#131C2F',
    border: '#233049',
    inputBackground: '#0F1729',
    chipBackground: '#1E293B',
    text: '#F1F5F9',
    textSecondary: '#A5B0C2',
    textTertiary: '#64748B',
    accent: '#10B981',
    accentSoft: 'rgba(16, 185, 129, 0.14)',
    accentText: '#34D399',
    info: '#818CF8',
    infoSoft: 'rgba(99, 102, 241, 0.16)',
    warning: '#FBBF24',
    warningSoft: 'rgba(245, 158, 11, 0.14)',
    danger: '#F87171',
    dangerSoft: 'rgba(239, 68, 68, 0.14)',
    neutralSoft: '#1B2537',
  },
} as const;

export type ThemeColors = (typeof Colors)['light'] | (typeof Colors)['dark'];

export type ScoreBand = 'great' | 'good' | 'mid' | 'low' | 'none';

export function scoreBand(score: number | null | undefined): ScoreBand {
  if (score === null || score === undefined) return 'none';
  if (score >= 85) return 'great';
  if (score >= 60) return 'good';
  if (score >= 40) return 'mid';
  return 'low';
}

export function scoreColors(colors: ThemeColors, score: number | null | undefined): {
  fg: string;
  bg: string;
} {
  switch (scoreBand(score)) {
    case 'great':
      return { fg: colors.accentText, bg: colors.accentSoft };
    case 'good':
      return { fg: colors.info, bg: colors.infoSoft };
    case 'mid':
      return { fg: colors.warning, bg: colors.warningSoft };
    case 'low':
      return { fg: colors.textSecondary, bg: colors.neutralSoft };
    default:
      return { fg: colors.textTertiary, bg: colors.neutralSoft };
  }
}

export const Fonts = Platform.select({
  ios: {
    sans: 'system-ui',
    serif: 'ui-serif',
    rounded: 'ui-rounded',
    mono: 'ui-monospace',
  },
  default: {
    sans: 'normal',
    serif: 'serif',
    rounded: 'normal',
    mono: 'monospace',
  },
  web: {
    sans: 'var(--font-display)',
    serif: 'var(--font-serif)',
    rounded: 'var(--font-rounded)',
    mono: 'var(--font-mono)',
  },
});
