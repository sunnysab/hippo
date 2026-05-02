import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { escapeHtml } from '../../utils/format';
import { ItemShowTypeBadge } from './ItemShowTypeBadge';

interface ArticleTypeFacetsProps {
  activeType: string;
  onChange: (type: string) => void;
}

export function ArticleTypeFacets({ activeType, onChange }: ArticleTypeFacetsProps) {
  const { state, dispatch } = useArticlesState();
  const { t } = useI18n();

  const payload = state.lastFacetPayload;
  if (!payload) return null;

  const facets = Array.isArray(payload.item_show_type_facets) ? payload.item_show_type_facets : [];
  if (!facets.length) {
    return <div className="article-type-facets" id="article-type-facets"><div className="article-type-facets-empty">{t('articles.summary.noTypeData', 'No type data available.')}</div></div>;
  }

  const allCount = facets.reduce((sum, item) => sum + (Number(item.count) || 0), 0);

  const items = [
    { value: '', count: allCount },
    ...facets.map((f) => ({
      value: String(f.item_show_type),
      count: Number(f.count || 0),
    })),
  ];
  const typeItems = items.filter((item) => item.value !== '');
  const activeItem = typeItems.find((item) => item.value === activeType) || null;
  const visibleItems = state.typeFacetsExpanded
    ? typeItems
    : (activeItem ? [activeItem] : []);
  const hiddenCount = typeItems.length - visibleItems.length;
  const isCollapsible = hiddenCount > 0;

  return (
    <div
      className={`article-type-facets${state.typeFacetsExpanded ? ' is-expanded' : ''}`}
      id="article-type-facets"
    >
      <button
        className={`article-type-facet${activeType ? '' : ' is-active'}`}
        type="button"
        aria-pressed={activeType ? 'false' : 'true'}
        onClick={() => onChange('')}
      >
        <span className="item-show-type-badge item-show-type-share item-show-type-badge-compact">
          {escapeHtml(t('filters.allTypes', 'All Types'))}
        </span>
        <span className="article-type-facet-count">{escapeHtml(allCount.toLocaleString('zh-CN'))}</span>
      </button>
      {visibleItems.map((item) => {
        const typeValue = Number(item.value);
        return (
          <button
            key={item.value}
            className={`article-type-facet${activeType === item.value ? ' is-active' : ''}`}
            type="button"
            aria-pressed={activeType === item.value ? 'true' : 'false'}
            onClick={() => onChange(item.value)}
          >
            <ItemShowTypeBadge value={typeValue} compact />
            <span className="article-type-facet-count">{escapeHtml((item.count || 0).toLocaleString('zh-CN'))}</span>
          </button>
        );
      })}
      {isCollapsible && (
        <button
          className="article-type-facet article-type-facet-toggle"
          type="button"
          aria-expanded={state.typeFacetsExpanded}
          onClick={() => dispatch({ type: 'SET_TYPE_FACETS_EXPANDED', expanded: !state.typeFacetsExpanded })}
        >
          <span>
            {state.typeFacetsExpanded
              ? t('articles.typeFacetCollapse', 'Collapse')
              : t('articles.typeFacetExpand', 'Expand all').replace('{n}', hiddenCount.toLocaleString('zh-CN'))
            }
          </span>
        </button>
      )}
    </div>
  );
}
