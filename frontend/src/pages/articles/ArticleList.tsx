import { useRef, useEffect, useCallback, useState } from 'react';
import { ContextMenu } from '../../components/ContextMenu';
import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { ArticleCard } from './ArticleCard';
import { EmptyState } from '../../components/EmptyState';
import { useToast } from '../../hooks/useToast';
import { copyToClipboard } from '../../utils/clipboard';
import { apiGet, apiSend, isAuthError } from '../../api';
import type { Article } from '../../store/articles';

interface ArticleListProps {
  onSelect: (id: number) => Promise<void>;
  onLoadMore: () => Promise<void>;
  onFilterByAccount?: (biz: string) => void;
}

export function ArticleList({ onSelect, onLoadMore, onFilterByAccount }: ArticleListProps) {
  const { state, dispatch } = useArticlesState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const loadMoreRef = useRef<HTMLButtonElement>(null);
  const [contextMenu, setContextMenu] = useState<{ article: Article; x: number; y: number } | null>(null);
  const [refetchingId, setRefetchingId] = useState<number | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleToggleStar = useCallback(async (article: Article) => {
    const starred = !article.starred_at;
    try {
      const result = await apiSend(`/api/article/${article.id}/star`, 'PUT', { starred }) as { starred_at?: string | null };
      dispatch({
        type: 'SET_ARTICLE_STAR',
        id: article.id,
        starredAt: starred ? result.starred_at ?? new Date().toISOString() : null,
      });
    } catch (err) {
      if (isAuthError(err)) return;
      showToast((err as Error)?.message || t('articles.starFailed', 'Failed to update star.'));
    }
  }, [dispatch, showToast, t]);

  const handleToggleRead = useCallback(async (article: Article) => {
    const read = !article.read_at;
    try {
      const result = await apiSend(`/api/article/${article.id}/read`, 'PUT', { read }) as { read_at?: string | null };
      dispatch({
        type: 'SET_ARTICLE_READ',
        id: article.id,
        readAt: read ? result.read_at ?? new Date().toISOString() : null,
      });
    } catch (err) {
      if (isAuthError(err)) return;
      showToast((err as Error)?.message || t('articles.readFailed', 'Failed to update read state.'));
    }
  }, [dispatch, showToast, t]);

  const handleRefetch = useCallback(async (article: Article) => {
    const articleId = article.id;
    setRefetchingId(articleId);
    setContextMenu(null);
    showToast(t('articles.refetchStarted', 'Refetching…'));
    try {
      const result = await apiSend(`/api/article/${articleId}/refetch`, 'POST', {}) as Record<string, unknown>;
      const taskId = result.task_id as string;
      if (!taskId) throw new Error('No task ID returned');

      const startedAt = Date.now();
      const pollInterval = 500;
      const maxDuration = 120_000;

      await new Promise<void>((resolve, reject) => {
        const poll = async () => {
          if (Date.now() - startedAt > maxDuration) {
            reject(new Error('Refetch timed out'));
            return;
          }
          try {
            const status = await apiGet(`/api/article/refetch/${taskId}`) as Record<string, unknown>;
            if (status.status === 'done') {
              resolve();
              return;
            }
            if (status.status === 'error') {
              reject(new Error((status.error as string) || 'Unknown error'));
              return;
            }
            pollTimerRef.current = setTimeout(poll, pollInterval);
          } catch (err) {
            if (isAuthError(err)) { reject(err); return; }
            pollTimerRef.current = setTimeout(poll, pollInterval);
          }
        };
        pollTimerRef.current = setTimeout(poll, pollInterval);
      });

      showToast(t('articles.refetchDone', 'Refetch completed.'));
      void onSelect(articleId);
    } catch (err) {
      if (isAuthError(err)) return;
      showToast((err as Error)?.message || t('articles.refetchFailed', 'Refetch failed.'));
    } finally {
      setRefetchingId(null);
      if (pollTimerRef.current) {
        clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    }
  }, [onSelect, showToast, t]);

  const handleLoadMore = useCallback(async () => {
    if (state.isArticleLoading || !state.hasMoreArticles) return;
    try {
      await onLoadMore();
    } catch (err) {
      if (isAuthError(err)) return;
      showToast((err as Error)?.message || t('articles.loadMoreFailed', 'Failed to load more articles.'));
    }
  }, [onLoadMore, showToast, t, state.hasMoreArticles, state.isArticleLoading]);

  useEffect(() => {
    return () => {
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
  }, []);

  // Watch the button instead of a scroller: on desktop the list clips itself and
  // on phones the page scrolls, so no single scroll target covers both.
  const loadMoreHandlerRef = useRef(handleLoadMore);
  useEffect(() => { loadMoreHandlerRef.current = handleLoadMore; }, [handleLoadMore]);

  // Re-observed when the list grows or the last page arrives, which is also what
  // makes a sentinel that is still on screen fill the viewport by pulling the
  // next page. The per-effect flag drops notifications already queued when this
  // observer was torn down, so a stale callback cannot load after unmount or
  // after a rebuild.
  useEffect(() => {
    const el = loadMoreRef.current;
    if (!el || typeof IntersectionObserver === 'undefined') return;
    let active = true;
    const observer = new IntersectionObserver((entries) => {
      if (!active) return;
      if (entries.some((entry) => entry.isIntersecting)) {
        void loadMoreHandlerRef.current();
      }
    });
    observer.observe(el);
    return () => {
      active = false;
      observer.disconnect();
    };
  }, [state.articles.length, state.hasMoreArticles]);

  if (!state.articles.length) {
    return (
      <div className="article-list-scroll" id="article-list">
        <EmptyState message={t('articles.emptyList', 'No articles found.')} />
      </div>
    );
  }

  return (
    <>
      <div className="article-list-scroll" id="article-list">
        {state.articles.map((article) => (
          <ArticleCard
            key={article.id}
            article={article}
            isActive={state.selectedArticleId === article.id}
            onClick={() => { void onSelect(article.id); }}
            onToggleRead={(target) => { void handleToggleRead(target); }}
            onToggleStar={(target) => { void handleToggleStar(target); }}
            onContextMenu={(e) => {
              e.preventDefault();
              setContextMenu({ article, x: e.clientX, y: e.clientY });
            }}
            onAccountClick={onFilterByAccount}
          />
        ))}
        {state.hasMoreArticles ? (
          <button
            ref={loadMoreRef}
            className="btn ghost article-list-more"
            id="btn-article-load-more"
            type="button"
            disabled={state.isArticleLoading}
            onClick={() => { void handleLoadMore(); }}
          >
            {state.isArticleLoading ? t('common.loading', 'Loading…') : t('articles.loadMore', 'Load more')}
          </button>
        ) : null}
      </div>
      <ContextMenu
        id="article-context-menu"
        isOpen={Boolean(contextMenu)}
        x={contextMenu?.x || 0}
        y={contextMenu?.y || 0}
        onClose={() => setContextMenu(null)}
      >
        {contextMenu ? (
          <button
            className="context-item"
            id="article-menu-open"
            type="button"
            disabled={!contextMenu.article.source_url && !contextMenu.article.link}
            onClick={() => {
              const link = contextMenu.article.source_url || contextMenu.article.link || '';
              if (link) window.open(link, '_blank', 'noopener,noreferrer');
              setContextMenu(null);
            }}
          >
            {t('articles.menu.openOriginal', 'Open original')}
          </button>
        ) : null}
        {contextMenu ? (
          <button
            className="context-item"
            id="article-menu-copy"
            type="button"
            disabled={!contextMenu.article.source_url && !contextMenu.article.link}
            onClick={async () => {
              const link = contextMenu.article.source_url || contextMenu.article.link || '';
              if (!link) return;
              try { await copyToClipboard(link); showToast(t('articles.linkCopied', 'Link copied.')); } catch { showToast(link); }
              setContextMenu(null);
            }}
          >
            {t('articles.menu.copyLink', 'Copy link')}
          </button>
        ) : null}
        {contextMenu ? (
          <button
            className="context-item"
            id="article-menu-refetch"
            type="button"
            disabled={!contextMenu.article.link || refetchingId === contextMenu.article.id}
            onClick={() => { void handleRefetch(contextMenu.article); }}
          >
            {refetchingId === contextMenu.article.id ? '…' : t('articles.menu.refetch', 'Re-fetch')}
          </button>
        ) : null}
      </ContextMenu>
    </>
  );
}
