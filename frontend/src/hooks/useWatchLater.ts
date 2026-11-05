import { useEffect, useRef, useState } from 'react';
import { apiSend, isAuthError } from '../api';
import { useArticlesState, type Article } from '../store/articles';
import { useToast } from './useToast';
import { useI18n } from '../i18n';

export type WatchLaterMove = 'up' | 'down' | 'top';

export function useWatchLater(onChanged: () => Promise<void>) {
  const { dispatch } = useArticlesState();
  const { showToast } = useToast();
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const changed = useRef(onChanged);
  useEffect(() => { changed.current = onChanged; }, [onChanged]);

  const change = async (article: Article, direction?: WatchLaterMove) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const result = await apiSend(`/api/watch-later/${article.id}`,
        direction ? 'PATCH' : article.watch_later_at ? 'DELETE' : 'PUT', direction ? { direction } : {});
      if (!direction) {
        dispatch({ type: 'SET_ARTICLE_WATCH_LATER', id: article.id, addedAt: result.watch_later_at as string | null });
      }
      await changed.current();
    } catch (error) {
      if (!isAuthError(error)) showToast((error as Error).message || t('articles.watchLaterFailed', 'Failed to update Watch Later.'));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  return { busy, toggle: (article: Article) => change(article), move: (article: Article, direction: WatchLaterMove) => change(article, direction) };
}
