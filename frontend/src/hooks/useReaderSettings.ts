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
  width: '860',
  font: '17',
  lineHeight: '1.8',
  letter: '0.2',
  serif: false,
  hideSmall: false,
};

const STORAGE_KEY = 'hippo-reader';

export function useReaderSettings() {
  const [config, setConfig] = useState<ReaderConfig>(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) return { ...defaultConfig, ...JSON.parse(saved) };
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
