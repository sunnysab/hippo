import { useArticlesState } from '../../store/articles';
import { useI18n } from '../../i18n';
import { escapeHtml } from '../../utils/format';

interface ArticleTopicFacetsProps {
  /** Comma-separated slugs, '' for every direction. */
  activeTopic: string;
  onChange: (topic: string) => void;
}

/**
 * The direction filter, one bar per vocabulary entry.
 *
 * Unlike the layout facets there is nothing to expand: the vocabulary is short
 * and an admin decides its length, so a fixed list with a count each is enough.
 */
export function ArticleTopicFacets({ activeTopic, onChange }: ArticleTopicFacetsProps) {
  const { state } = useArticlesState();
  const { t } = useI18n();

  const facets = Array.isArray(state.lastFacetPayload?.topic_facets)
    ? state.lastFacetPayload!.topic_facets!
    : [];
  if (!facets.length) return null;

  const active = new Set(activeTopic ? activeTopic.split(',') : []);
  const total = facets.reduce((sum, item) => sum + (Number(item.count) || 0), 0);

  const toggle = (slug: string) => {
    const next = new Set(active);
    if (next.has(slug)) next.delete(slug);
    else next.add(slug);
    // Keep vocabulary order so the URL and the visible selection stay in step.
    onChange(facets.filter((item) => next.has(item.slug)).map((item) => item.slug).join(','));
  };

  return (
    <div className="article-type-facets" id="article-topic-facets">
      <button
        className={`article-type-facet${active.size ? '' : ' is-active'}`}
        type="button"
        aria-pressed={active.size ? 'false' : 'true'}
        onClick={() => onChange('')}
      >
        <span className="item-show-type-badge item-show-type-share item-show-type-badge-compact">
          {escapeHtml(t('filters.allTopics', 'All directions'))}
        </span>
        <span className="article-type-facet-count">{escapeHtml(total.toLocaleString('zh-CN'))}</span>
      </button>
      {facets.map((item) => (
        <button
          key={item.slug}
          className={`article-type-facet${active.has(item.slug) ? ' is-active' : ''}`}
          type="button"
          aria-pressed={active.has(item.slug) ? 'true' : 'false'}
          onClick={() => toggle(item.slug)}
        >
          <span className="item-show-type-badge item-show-type-share item-show-type-badge-compact">
            {escapeHtml(item.name)}
          </span>
          <span className="article-type-facet-count">
            {escapeHtml((Number(item.count) || 0).toLocaleString('zh-CN'))}
          </span>
        </button>
      ))}
    </div>
  );
}
