import { useI18n } from '../../i18n';
import { escapeHtml, formatDateTime } from '../../utils/format';
import { ITEM_SHOW_TYPE_META } from '../../utils/constants';
import type { Article } from '../../store/articles';

interface ArticleHeaderProps {
  article: Article;
}

export function ArticleHeader({ article }: ArticleHeaderProps) {
  const { t } = useI18n();

  const getTypeLabel = (value: number | null) => {
    if (value === null || value === undefined) return t('articles.meta.unknown', 'Unknown');
    const meta = ITEM_SHOW_TYPE_META[value];
    return meta ? t(meta.key, meta.fallback) : t('articles.meta.unknown', 'Unknown');
  };

  const getTypeBadge = (value: number | null) => {
    if (value === null || value === undefined) return '';
    const meta = ITEM_SHOW_TYPE_META[value];
    if (!meta) return '';
    return `<span class="item-show-type-badge item-show-type-${meta.tone}">${escapeHtml(getTypeLabel(value))}</span>`;
  };

  const avatarUrl = article.account_avatar_url || '';

  return (
    <div className="article-header">
      <div className="article-preview-title-row">
        <h1 className="article-preview-title">{article.title || ''}</h1>
        <div className="article-preview-type" dangerouslySetInnerHTML={{ __html: getTypeBadge(article.item_show_type) }} />
      </div>
      <div className="article-preview-meta">
        <div className="article-preview-account">
          {avatarUrl ? (
            <img className="article-preview-avatar" src={avatarUrl} alt={article.account_nickname || ''} onError={(e) => { (e.target as HTMLImageElement).style.display = 'none'; }} />
          ) : (
            <div className="article-preview-avatar placeholder"></div>
          )}
          <span className="article-preview-name">
            {article.account_nickname || article.account_alias || article.biz || ''}
          </span>
        </div>
        <div className="article-preview-items">
          <div className="article-preview-item">
            <span className="article-preview-label">{t('articles.meta.author', 'Author')}</span>
            <span className="article-preview-value">{article.author || t('articles.meta.unknown', 'Unknown')}</span>
          </div>
          <div className="article-preview-item">
            <span className="article-preview-label">{t('articles.meta.type', 'Type')}</span>
            <span className="article-preview-value">{getTypeLabel(article.item_show_type)}</span>
          </div>
          <div className="article-preview-item">
            <span className="article-preview-label">{t('articles.meta.publishedAt', 'Published')}</span>
            <span className="article-preview-value">{formatDateTime(article.publish_at || article.created_at)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
