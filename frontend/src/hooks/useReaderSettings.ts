import { useState, useCallback, useEffect } from 'react';

export interface ReaderConfig {
  width: string;
  font: string;
  lineHeight: string;
  letter: string;
  serif: boolean;
  hideSmall: boolean;
}

const defaultConfig: ReaderConfig = {
  width: '760',
  font: '17',
  lineHeight: '1.8',
  letter: '0.2',
  serif: false,
  hideSmall: false,
};

/**
 * Bounds live here so a stored config from an older release cannot keep a width
 * the sliders no longer offer.
 */
export const READER_WIDTH_RANGE = { min: 520, max: 900 };
export const READER_LETTER_RANGE = { min: 0, max: 0.3 };

const clamp = (value: string, min: number, max: number, fallback: number): string => {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return String(fallback);
  return String(Math.min(Math.max(parsed, min), max));
};

/** The width that shipped before this release. Nobody picked it, so it moves. */
const LEGACY_WIDTH_DEFAULT = '860';

export const normalizeReaderConfig = (config: ReaderConfig): ReaderConfig => ({
  ...config,
  width: config.width === LEGACY_WIDTH_DEFAULT
    ? defaultConfig.width
    : clamp(config.width, READER_WIDTH_RANGE.min, READER_WIDTH_RANGE.max, 760),
  letter: clamp(config.letter, READER_LETTER_RANGE.min, READER_LETTER_RANGE.max, 0.2),
});

const STORAGE_KEY = 'hippo-reader';

export function useReaderSettings() {
  const [config, setConfig] = useState<ReaderConfig>(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) return normalizeReaderConfig({ ...defaultConfig, ...JSON.parse(saved) });
    } catch { /* ignore */ }
    return { ...defaultConfig };
  });

  useEffect(() => {
    const root = document.documentElement;
    root.style.setProperty('--reader-width', `${config.width}px`);
    root.style.setProperty('--reader-font', `${config.font}px`);
    root.style.setProperty('--reader-line', config.lineHeight);
    root.style.setProperty('--reader-letter', `${config.letter}px`);
    root.style.setProperty('--reader-family', config.serif ? 'var(--font-serif)' : 'var(--font-sans)');
    localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
  }, [config]);

  const updateConfig = useCallback((patch: Partial<ReaderConfig>) => {
    setConfig((prev) => ({ ...prev, ...patch }));
  }, []);

  return { config, updateConfig };
}
