import { useI18n } from '../../i18n';
import { formatDateTime } from '../../utils/format';
import { getSourceLabel, getSourceTone } from '../../utils/source';
import type { Article } from '../../store/articles';
import { getItemShowTypeLabel } from './itemShowType';

interface ArticleHeaderProps {
  article: Article;
  /** Jumps to the same story's other reports; absent when the list cannot filter. */
  onOpenEvent?: (eventId: number) => void;
}

export function ArticleHeader({ article, onOpenEvent }: ArticleHeaderProps) {
  const { t } = useI18n();

  const avatarUrl = article.account_avatar_url || '';
  const sourceLabel = getSourceLabel(article.source, t);
  // Absent until the background classifier has seen this article.
  const topics = Array.isArray(article.topics) ? article.topics : [];
  // Only present once a second publisher reported the same story.
  const event = article.event || null;

  return (
    <div className="article-header">
      <h1 className="article-preview-title">{article.title || ''}</h1>
      {event ? (
        <div className="article-event">
          <span className="article-event-title">{event.title}</span>
          <span className="article-event-count">
            {t('articles.event.reports', '这件事共 {n} 篇报道').replace('{n}', String(event.article_count))}
          </span>
          {onOpenEvent ? (
            <button className="btn ghost article-event-open" type="button" onClick={() => onOpenEvent(event.id)}>
              {t('articles.event.open', '看这件事的其它报道')}
            </button>
          ) : null}
        </div>
      ) : null}
      <div className="article-preview-meta">
        <div className="article-preview-account">
          {avatarUrl ? (
            <img className="article-preview-avatar" src={avatarUrl} alt={article.account_nickname || ''} onError={(e) => { (e.target as HTMLImageElement).style.visibility = 'hidden'; }} />
          ) : (
            <div className="article-preview-avatar placeholder"></div>
          )}
          <span className="article-preview-name">
            {article.account_nickname || article.account_alias || article.biz || ''}
          </span>
        </div>
        <div className="article-preview-items">
          {sourceLabel ? (
            <span className={`item-show-type-badge item-show-type-${getSourceTone(article.source)}`}>
              {sourceLabel}
            </span>
          ) : null}
          <div className="article-preview-item">
            <span className="article-preview-label">{t('articles.meta.author', 'Author')}</span>
            <span className="article-preview-value">{article.author || t('articles.meta.unknown', 'Unknown')}</span>
          </div>
          <div className="article-preview-item">
            <span className="article-preview-label">{t('articles.meta.type', 'Type')}</span>
            <span className="article-preview-value">{getItemShowTypeLabel(article.item_show_type, t)}</span>
          </div>
          <div className="article-preview-item">
            <span className="article-preview-label">{t('articles.meta.publishedAt', 'Published')}</span>
            <span className="article-preview-value">{formatDateTime(article.publish_at || article.created_at)}</span>
          </div>
          {topics.length ? (
            <div className="article-preview-item">
              <span className="article-preview-label">{t('articles.meta.directions', '方向')}</span>
              <span className="article-preview-value article-preview-topics">
                {topics.map((topic) => (
                  <span className="topic-chip" key={topic.slug}>{topic.name}</span>
                ))}
              </span>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
