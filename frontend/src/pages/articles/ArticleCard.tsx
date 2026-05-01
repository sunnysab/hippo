import { memo } from 'react';
import { escapeHtml, formatDate } from '../../utils/format';
import type { Article } from '../../store/articles';
import { ItemShowTypeBadge } from './ItemShowTypeBadge';

interface ArticleCardProps {
  article: Article;
  isActive: boolean;
  onClick: () => void;
  onContextMenu: (e: React.MouseEvent) => void;
}

export const ArticleCard = memo(function ArticleCard({
  article,
  isActive,
  onClick,
  onContextMenu,
}: ArticleCardProps) {
  const thumb = article.image_id ? `/api/image/${article.image_id}` : '';
  const avatar = article.account_avatar_url || '';
  const digest = article.digest || '';

  return (
    <div
      className={`article-card${isActive ? ' is-active' : ''}`}
      role="button"
      tabIndex={0}
      onClick={onClick}
      onContextMenu={onContextMenu}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onClick();
        }
      }}
    >
      {thumb ? (
        <img className="article-thumb" src={thumb} alt="" onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }} />
      ) : (
        <div className="article-thumb placeholder"></div>
      )}
      <div className="article-info">
        <div className="article-title-row">
          <div className="article-title">{escapeHtml(article.title || '')}</div>
          <ItemShowTypeBadge value={article.item_show_type} compact />
        </div>
        <div className="article-meta">
          {avatar ? (
            <img className="article-avatar" src={avatar} alt="" onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }} />
          ) : null}
          <span>{escapeHtml(article.account_nickname || '')}</span>
          <span>{escapeHtml(formatDate(article.publish_at))}</span>
        </div>
        <div className="article-digest" title={digest}>{digest}</div>
      </div>
    </div>
  );
});
