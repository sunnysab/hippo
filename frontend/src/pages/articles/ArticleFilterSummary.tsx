import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { escapeHtml } from '../../utils/format';
import { ITEM_SHOW_TYPE_META } from '../../utils/constants';

export function ArticleFilterSummary() {
  const { state } = useArticlesState();
  const { t } = useI18n();

  const payload = state.lastFacetPayload;
  const total = Number(payload?.total || 0);
  const typeFilter = (document.getElementById('article-type-filter') as HTMLSelectElement)?.value || '';
  const search = (document.getElementById('article-search') as HTMLInputElement)?.value?.trim() || '';
  const groupLabel = (document.getElementById('article-group-filter') as HTMLSelectElement)?.selectedOptions?.[0]?.textContent || '';
  const accountLabel = (document.getElementById('article-account-filter') as HTMLSelectElement)?.selectedOptions?.[0]?.textContent || '';

  const totalLabel = t('articles.summary.total', '{n} articles').replace('{n}', total.toLocaleString('zh-CN'));
  const tags: string[] = [];
  if (groupLabel && (document.getElementById('article-group-filter') as HTMLSelectElement)?.value) {
    tags.push(t('articles.summary.group', 'Group: {value}').replace('{value}', groupLabel));
  }
  if (accountLabel && (document.getElementById('article-account-filter') as HTMLSelectElement)?.value) {
    tags.push(t('articles.summary.account', 'Account: {value}').replace('{value}', accountLabel));
  }
  if (typeFilter) {
    const meta = ITEM_SHOW_TYPE_META[Number(typeFilter)];
    const label = meta ? t(meta.key, meta.fallback) : '';
    tags.push(t('articles.summary.filteredByType', 'Type: {type}').replace('{type}', label));
  }
  if (search) {
    tags.push(t('articles.summary.keyword', 'Search: {value}').replace('{value}', search));
  }
  if (!tags.length) {
    tags.push(t('articles.summary.allTypes', 'Across all article types'));
  }

  return (
    <div className="article-filter-summary" id="article-filter-summary">
      <strong>{escapeHtml(totalLabel)}</strong>
      {tags.map((tag, i) => (
        <span key={i} className="article-summary-pill">{escapeHtml(tag)}</span>
      ))}
    </div>
  );
}
