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
  const { state } = useArticlesState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const listRef = useRef<HTMLDivElement>(null);
  const [contextMenu, setContextMenu] = useState<{ article: Article; x: number; y: number } | null>(null);
  const [refetchingId, setRefetchingId] = useState<number | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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

  const handleLoadMore = useCallback(() => {
    if (state.isArticleLoading || !state.hasMoreArticles) return;
    void onLoadMore();
  }, [onLoadMore, state.hasMoreArticles, state.isArticleLoading]);

  useEffect(() => {
    return () => {
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
  }, []);

  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const handler = () => {
      if (el.scrollTop + el.clientHeight >= el.scrollHeight - 50) {
        handleLoadMore();
      }
    };
    el.addEventListener('scroll', handler);
    return () => el.removeEventListener('scroll', handler);
  }, [handleLoadMore]);

  if (!state.articles.length) {
    return (
      <div className="article-list-scroll" id="article-list" ref={listRef}>
        <EmptyState message={t('articles.emptyList', 'No articles found.')} />
      </div>
    );
  }

  return (
    <>
      <div className="article-list-scroll" id="article-list" ref={listRef}>
        {state.articles.map((article) => (
          <ArticleCard
            key={article.id}
            article={article}
            isActive={state.selectedArticleId === article.id}
            onClick={() => { void onSelect(article.id); }}
            onContextMenu={(e) => {
              e.preventDefault();
              setContextMenu({ article, x: e.clientX, y: e.clientY });
            }}
            onAccountClick={onFilterByAccount}
          />
        ))}
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
