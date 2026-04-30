import { memo } from 'react';
import { escapeHtml, formatDate } from '../../utils/format';
import { ITEM_SHOW_TYPE_META } from '../../utils/constants';
import { useI18n } from '../../i18n';
import type { Article } from '../../store/articles';

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
  const { t } = useI18n();

  const getTypeBadge = (value: number | null): string => {
    if (value === null || value === undefined) return '';
    const meta = ITEM_SHOW_TYPE_META[value];
    if (!meta) return '';
    const label = t(meta.key, meta.fallback);
    return `<span class="item-show-type-badge item-show-type-${meta.tone} item-show-type-badge-compact">${escapeHtml(label)}</span>`;
  };

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
          <span dangerouslySetInnerHTML={{ __html: getTypeBadge(article.item_show_type) }} />
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
