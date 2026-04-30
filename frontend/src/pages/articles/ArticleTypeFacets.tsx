import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { escapeHtml } from '../../utils/format';
import { ITEM_SHOW_TYPE_META, ARTICLE_FACET_COLLAPSED_LIMIT, ARTICLE_FACET_COLLAPSED_LIMIT_MOBILE } from '../../utils/constants';
import { buildArticleFacetVisibility } from '../../utils/facets';

function getItemShowTypeLabel(value: number, t: (k: string, f: string) => string) {
  const meta = ITEM_SHOW_TYPE_META[value];
  if (!meta) return t('articles.meta.unknown', 'Unknown');
  return t(meta.key, meta.fallback);
}

function renderBadge(value: number, t: (k: string, f: string) => string, compact = false) {
  const meta = ITEM_SHOW_TYPE_META[value];
  if (!meta) return '';
  const compactClass = compact ? ' item-show-type-badge-compact' : '';
  return `<span class="item-show-type-badge item-show-type-${meta.tone}${compactClass}">${escapeHtml(getItemShowTypeLabel(value, t))}</span>`;
}

interface ArticleTypeFacetsProps {
  activeType: string;
  onChange: (type: string) => void;
}

export function ArticleTypeFacets({ activeType, onChange }: ArticleTypeFacetsProps) {
  const { state, dispatch } = useArticlesState();
  const { t } = useI18n();
  const isNarrow = window.matchMedia('(max-width: 720px)').matches;

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

  const collapsedLimit = isNarrow ? ARTICLE_FACET_COLLAPSED_LIMIT_MOBILE : ARTICLE_FACET_COLLAPSED_LIMIT;
  const visibility = buildArticleFacetVisibility({
    items,
    activeValue: activeType,
    collapsedLimit,
    expanded: state.typeFacetsExpanded,
  });

  return (
    <div className="article-type-facets" id="article-type-facets">
      <button
        className={`article-type-facet${activeType ? '' : ' is-active'}`}
        type="button"
        onClick={() => onChange('')}
      >
        <span className="item-show-type-badge item-show-type-share item-show-type-badge-compact">
          {escapeHtml(t('filters.allTypes', 'All Types'))}
        </span>
        <span className="article-type-facet-count">{escapeHtml(allCount.toLocaleString('zh-CN'))}</span>
      </button>
      {visibility.visibleItems.filter((item) => item.value !== '').map((item) => {
        const typeValue = Number(item.value);
        return (
          <button
            key={item.value}
            className={`article-type-facet${activeType === item.value ? ' is-active' : ''}`}
            type="button"
            onClick={() => onChange(item.value)}
            dangerouslySetInnerHTML={{
              __html: `${renderBadge(typeValue, t, true)} <span class="article-type-facet-count">${escapeHtml((item.count || 0).toLocaleString('zh-CN'))}</span>`,
            }}
          />
        );
      })}
      {visibility.isCollapsible && (
        <button
          className="article-type-facet article-type-facet-toggle"
          type="button"
          aria-expanded={state.typeFacetsExpanded}
          onClick={() => dispatch({ type: 'SET_TYPE_FACETS_EXPANDED', expanded: !state.typeFacetsExpanded })}
        >
          <span>
            {state.typeFacetsExpanded
              ? t('articles.typeFacetCollapse', 'Collapse')
              : t('articles.typeFacetExpand', 'Show {n} more').replace('{n}', visibility.hiddenCount.toLocaleString('zh-CN'))
            }
          </span>
        </button>
      )}
    </div>
  );
}
