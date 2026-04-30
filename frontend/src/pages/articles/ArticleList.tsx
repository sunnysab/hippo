import { useRef, useEffect, useCallback, useState } from 'react';
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
}

export function ArticleList({ onSelect }: ArticleListProps) {
  const { state } = useArticlesState();
  const { t } = useI18n();
  const { showToast } = useToast();
  const listRef = useRef<HTMLDivElement>(null);
  const [contextMenu, setContextMenu] = useState<{ article: Article; x: number; y: number } | null>(null);
  const [imageCtxMenu, setImageCtxMenu] = useState<{ imageId: number; x: number; y: number } | null>(null);

  const handleLoadMore = useCallback(() => {
    if (state.isArticleLoading || !state.hasMoreArticles) return;
    window.dispatchEvent(new CustomEvent('hippo:refresh'));
  }, [state.isArticleLoading, state.hasMoreArticles]);

  // Infinite scroll
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

  // Close context menus on click/escape
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      setContextMenu(null);
      setImageCtxMenu(null);
    };
    const keyHandler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setContextMenu(null); setImageCtxMenu(null); }
    };
    document.addEventListener('click', handler);
    document.addEventListener('keydown', keyHandler);
    return () => { document.removeEventListener('click', handler); document.removeEventListener('keydown', keyHandler); };
  }, []);

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
      <div className="panel-footer">
        <button
          className="btn ghost"
          id="btn-article-more"
          type="button"
          style={{ display: 'none' }}
          onClick={handleLoadMore}
        >
          {t('articles.loadMore', 'Load More')}
        </button>
      </div>

      {/* Article context menu */}
      {contextMenu && (
        <div className="context-menu" id="article-context-menu" style={{ left: contextMenu.x, top: contextMenu.y }}>
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
        </div>
      )}
    </>
  );
}
