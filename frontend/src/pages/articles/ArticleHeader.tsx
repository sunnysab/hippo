import { useI18n } from '../../i18n';
import { formatDateTime } from '../../utils/format';
import type { Article } from '../../store/articles';
import { ItemShowTypeBadge } from './ItemShowTypeBadge';
import { getItemShowTypeLabel } from './itemShowType';

interface ArticleHeaderProps {
  article: Article;
}

export function ArticleHeader({ article }: ArticleHeaderProps) {
  const { t } = useI18n();

  const avatarUrl = article.account_avatar_url || '';

  return (
    <div className="article-header">
      <div className="article-preview-title-row">
        <h1 className="article-preview-title">{article.title || ''}</h1>
        <div className="article-preview-type">
          <ItemShowTypeBadge value={article.item_show_type} />
        </div>
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
            <span className="article-preview-value">{getItemShowTypeLabel(article.item_show_type, t)}</span>
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
