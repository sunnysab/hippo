import { createContext, useContext, useMemo, type ReactNode } from 'react';
import zhCN from './zh-CN.json';

type I18nDict = Record<string, string>;

const I18nContext = createContext<(key: string, fallback?: string) => string>(
  (key, fallback) => fallback || key,
);

export function I18nProvider({ children }: { children: ReactNode }) {
  const dict: I18nDict = zhCN as I18nDict;

  const t = useMemo(() => (
    (key: string, fallback?: string): string => {
      return dict[key] || fallback || key;
    }
  ), [dict]);

  return <I18nContext.Provider value={t}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  const t = useContext(I18nContext);
  return { t };
}
