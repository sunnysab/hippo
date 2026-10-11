/**
 * Where an article came from.
 *
 * Sources are an open set: the backend stores a discriminator string, not an
 * enum. Everything a screen needs to say about a source lives in this table, so
 * adding one is a data change and never a layout branch.
 */

export interface SourceMeta {
  /** i18n key and its fallback label. */
  key: string;
  fallback: string;
  /** Badge tone class suffix, matching the `.item-show-type-*` palette. */
  tone: string;
}

export const SOURCE_META: Record<string, SourceMeta> = {
  weixin: { key: 'source.weixin', fallback: '微信公众号', tone: 'regular' },
  bilibili: { key: 'source.bilibili', fallback: '哔哩哔哩', tone: 'video' },
  youtube: { key: 'source.youtube', fallback: 'YouTube', tone: 'share' },
  podcast: { key: 'source.podcast', fallback: '播客', tone: 'audio' },
};

/** Falls back to the raw identifier: an unknown source is still shown. */
export const getSourceLabel = (
  source: string | null | undefined,
  t: (key: string, fallback?: string) => string,
): string => {
  if (!source) return '';
  const meta = SOURCE_META[source];
  return meta ? t(meta.key, meta.fallback) : source;
};

export const getSourceTone = (source: string | null | undefined): string =>
  (source && SOURCE_META[source]?.tone) || 'regular';
