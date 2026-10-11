import { memo, useState } from 'react';
import { formatDuration } from '../../utils/duration';
import { escapeHtml, formatDate } from '../../utils/format';
import type { Article } from '../../store/articles';
import { ContentKindBadge } from './ContentKindBadge';
import { useI18n } from '../../i18n';

interface ArticleCardProps {
  article: Article;
  isActive: boolean;
  onClick: () => void;
  onContextMenu: (e: React.MouseEvent) => void;
  onToggleRead?: (article: Article) => void;
  onToggleStar?: (article: Article) => void;
  onAccountClick?: (biz: string) => void;
}

export const ArticleCard = memo(function ArticleCard({
  article,
  isActive,
  onClick,
  onContextMenu,
  onToggleRead,
  onToggleStar,
  onAccountClick,
}: ArticleCardProps) {
  const { t } = useI18n();
  const [thumbFailed, setThumbFailed] = useState(false);
  const thumb = article.image_id && !thumbFailed ? `/api/image/${article.image_id}` : '';
  const avatar = article.account_avatar_url || '';
  const digest = article.digest || '';
  const isRead = Boolean(article.read_at);
  const isStarred = Boolean(article.starred_at);
  const duration = formatDuration(article.duration_seconds);

  return (
    <div
      className={`article-card${isActive ? ' is-active' : ''}${isRead ? ' is-read' : ''}${isStarred ? ' is-starred' : ''}`}
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
      <div className="article-thumb-wrap">
        {thumb ? (
          <img className="article-thumb" src={thumb} alt="" onError={() => setThumbFailed(true)} />
        ) : (
          <div className="article-thumb placeholder"></div>
        )}
        {duration ? <span className="article-duration">{duration}</span> : null}
      </div>
      <div className="article-info">
        <div className="article-title">
          {!isRead ? <span className="article-unread-dot" aria-label={t('articles.unread', '未读')} /> : null}
          {escapeHtml(article.title || '')}
        </div>
        <div className="article-meta">
          {avatar ? (
            <img className="article-avatar" src={avatar} alt="" onError={(e) => { (e.target as HTMLImageElement).style.visibility = 'hidden'; }} />
          ) : null}
          <span
            className="article-account-name"
            title="按此公众号筛选"
            onClick={(e) => {
              e.stopPropagation();
              onAccountClick?.(article.biz);
            }}
          >{escapeHtml(article.account_nickname || '')}</span>
          <span>{escapeHtml(formatDate(article.publish_at))}</span>
          <ContentKindBadge kind={article.content_kind} compact />
        </div>
        <div className="article-digest" title={digest}>{digest}</div>
      </div>
      {onToggleStar || onToggleRead ? (
        <div className="article-card-actions">
          {onToggleStar ? (
            <button
              className={`article-card-action${isStarred ? ' is-on' : ''}`}
              type="button"
              title={isStarred ? t('articles.unstar', '取消收藏') : t('articles.star', '收藏')}
              aria-label={isStarred ? t('articles.unstar', '取消收藏') : t('articles.star', '收藏')}
              aria-pressed={isStarred}
              onClick={(e) => {
                e.stopPropagation();
                onToggleStar(article);
              }}
            >
              <span className="icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.5l2.95 6.1 6.7.86-4.9 4.63 1.24 6.66L12 17.6l-5.99 3.15L7.25 14.1 2.35 9.46l6.7-.86z"/></svg></span>
            </button>
          ) : null}
          {onToggleRead ? (
            <button
              className="article-card-action"
              type="button"
              title={isRead ? t('articles.markUnread', '标记未读') : t('articles.markRead', '标记已读')}
              aria-label={isRead ? t('articles.markUnread', '标记未读') : t('articles.markRead', '标记已读')}
              onClick={(e) => {
                e.stopPropagation();
                onToggleRead(article);
              }}
            >
              <span className="icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5c-5 0-9 4.5-9 7s4 7 9 7 9-4.5 9-7-4-7-9-7zm0 11a4 4 0 1 1 0-8 4 4 0 0 1 0 8zm0-2a2 2 0 1 0 0-4 2 2 0 0 0 0 4z"/></svg></span>
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
});
