import { useCallback, useEffect, useState } from 'react';
import { apiGet } from '../api';
import { createLatestOnly } from '../utils/latest';
import { onRefresh } from '../utils/events';

export interface ArticleSource {
  source: string;
  channels: number;
}

/** Share source choices between the wide rail and the compact toolbar. */
export function useArticleSources() {
  const [sources, setSources] = useState<ArticleSource[]>([]);
  const [error, setError] = useState(false);
  const [load] = useState(() => createLatestOnly<ArticleSource[] | null>((next) => {
    setError(next === null);
    if (next) setSources(next);
  }));
  const reload = useCallback(async () => {
    await load(async () => {
      try {
        const payload = await apiGet('/api/source') as { sources?: ArticleSource[] };
        return payload.sources || [];
      } catch {
        return null;
      }
    });
  }, [load]);

  useEffect(() => {
    void reload();
    return onRefresh(() => { void reload(); });
  }, [reload]);

  return { sources, error, reload };
}
