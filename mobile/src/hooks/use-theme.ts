import { Colors, type ThemeColors } from '@/constants/theme';

import { useColorScheme } from './use-color-scheme';

export function useTheme(): { colors: ThemeColors; dark: boolean } {
  const scheme = useColorScheme();
  const dark = scheme === 'dark';
  return { colors: dark ? Colors.dark : Colors.light, dark };
}
