import { useI18n } from '../../i18n';
import { escapeHtml } from '../../utils/format';
import { ITEM_SHOW_TYPE_META } from '../../utils/constants';
import type {
  ArticleFiltersState,
  ArticleSelectOption,
} from './filtering';

interface ArticleFilterSummaryProps {
  filters: ArticleFiltersState;
  groupOptions: ArticleSelectOption[];
  accountOptions: ArticleSelectOption[];
  total: number;
}

export function ArticleFilterSummary({
  filters,
  groupOptions,
  accountOptions,
  total,
}: ArticleFilterSummaryProps) {
  const { t } = useI18n();
  const groupLabel = groupOptions.find((group) => group.value === filters.groupId)?.label || '';
  const accountLabel = accountOptions.find((account) => account.value === filters.accountBiz)?.label || '';

  const totalLabel = t('articles.summary.total', '{n} articles').replace('{n}', total.toLocaleString('zh-CN'));
  const tags: string[] = [];
  if (groupLabel && filters.groupId) {
    tags.push(t('articles.summary.group', 'Group: {value}').replace('{value}', groupLabel));
  }
  if (accountLabel && filters.accountBiz) {
    tags.push(t('articles.summary.account', 'Account: {value}').replace('{value}', accountLabel));
  }
  if (filters.itemShowType) {
    const meta = ITEM_SHOW_TYPE_META[Number(filters.itemShowType)];
    const label = meta ? t(meta.key, meta.fallback) : '';
    tags.push(t('articles.summary.filteredByType', 'Type: {type}').replace('{type}', label));
  }
  if (filters.search.trim()) {
    tags.push(t('articles.summary.keyword', 'Search: {value}').replace('{value}', filters.search.trim()));
  }
  if (!tags.length) {
    tags.push(t('articles.summary.allTypes', 'Across all article types'));
  }

  return (
    <div className="article-filter-summary" id="article-filter-summary">
      <strong>{escapeHtml(totalLabel)}</strong>
      {tags.map((tag, i) => (
        <span key={i} className="meta-note">{escapeHtml(tag)}</span>
      ))}
    </div>
  );
}
