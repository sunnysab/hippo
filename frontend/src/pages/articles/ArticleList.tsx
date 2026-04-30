import { useRef, useEffect, useCallback, useState } from 'react';
import { ContextMenu } from '../../components/ContextMenu';
import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { ArticleCard } from './ArticleCard';
import { EmptyState } from '../../components/EmptyState';
import { useToast } from '../../hooks/useToast';
import { copyToClipboard } from '../../utils/clipboard';
import { apiGet } from '../../api';
import type { Article } from '../../store/articles';

interface ArticleListProps {
  onSelect: (id: number) => Promise<void>;
  onLoadMore: () => Promise<void>;
}

export function ArticleList({ onSelect, onLoadMore }: ArticleListProps) {
  const { state } = useArticlesState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const listRef = useRef<HTMLDivElement>(null);
  const [contextMenu, setContextMenu] = useState<{ article: Article; x: number; y: number } | null>(null);

  const handleLoadMore = useCallback(() => {
    if (state.isArticleLoading || !state.hasMoreArticles) return;
    void onLoadMore();
  }, [onLoadMore, state.hasMoreArticles, state.isArticleLoading]);

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
            disabled={!contextMenu.article.source_url && !(contextMenu.article as unknown as Record<string, string>).link}
            onClick={() => {
              const link = contextMenu.article.source_url || (contextMenu.article as unknown as Record<string, string>).link || '';
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
            disabled={!contextMenu.article.source_url && !(contextMenu.article as unknown as Record<string, string>).link}
            onClick={async () => {
              const link = contextMenu.article.source_url || (contextMenu.article as unknown as Record<string, string>).link || '';
              if (!link) return;
              try { await copyToClipboard(link); showToast(t('articles.linkCopied', 'Link copied.')); } catch { showToast(link); }
              setContextMenu(null);
            }}
          >
            {t('articles.menu.copyLink', 'Copy link')}
          </button>
        ) : null}
      </ContextMenu>
    </>
  );
}
