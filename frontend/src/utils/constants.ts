export const ITEM_SHOW_TYPE_META: Record<number, { key: string; fallback: string; tone: string }> = {
  0: { key: 'articles.type.0', fallback: 'Regular Article', tone: 'regular' },
  5: { key: 'articles.type.5', fallback: 'Video Share', tone: 'video' },
  6: { key: 'articles.type.6', fallback: 'Music Share', tone: 'music' },
  7: { key: 'articles.type.7', fallback: 'Audio Share', tone: 'audio' },
  8: { key: 'articles.type.8', fallback: 'Picture Share', tone: 'picture' },
  10: { key: 'articles.type.10', fallback: 'Text Share', tone: 'text' },
  11: { key: 'articles.type.11', fallback: 'Article Share', tone: 'share' },
  17: { key: 'articles.type.17', fallback: 'Short Post', tone: 'short' },
};

export const ARTICLE_SORT_PUBLISH_AT_DESC = 'publish_at_desc';
export const ARTICLE_SORT_RELEVANCE_DESC = 'relevance_desc';

export const ARTICLE_FACET_COLLAPSED_LIMIT = 5;
export const ARTICLE_FACET_COLLAPSED_LIMIT_MOBILE = 3;
