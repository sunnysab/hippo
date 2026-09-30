import { useCallback, useEffect, useState } from 'react';

export type ThemeMode = 'system' | 'light' | 'dark';

const STORAGE_KEY = 'hippo-theme';
const MODES: ThemeMode[] = ['system', 'light', 'dark'];

function readStored(): ThemeMode {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === 'light' || stored === 'dark' || stored === 'system') {
      return stored;
    }
  } catch {
    /* private mode: fall through to the default */
  }
  return 'system';
}

function apply(mode: ThemeMode) {
  // "system" means "no attribute", so the prefers-color-scheme block wins.
  if (mode === 'system') {
    document.documentElement.removeAttribute('data-theme');
  } else {
    document.documentElement.setAttribute('data-theme', mode);
  }
}

export function useTheme() {
  const [mode, setMode] = useState<ThemeMode>(readStored);

  useEffect(() => {
    apply(mode);
  }, [mode]);

  const cycle = useCallback(() => {
    setMode((current) => {
      const next = MODES[(MODES.indexOf(current) + 1) % MODES.length];
      try {
        window.localStorage.setItem(STORAGE_KEY, next);
      } catch {
        /* the attribute still applies for this session */
      }
      return next;
    });
  }, []);

  return { mode, cycle };
}
