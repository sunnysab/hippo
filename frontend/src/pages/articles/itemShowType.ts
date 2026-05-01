import { ITEM_SHOW_TYPE_META } from '../../utils/constants';

export const getItemShowTypeLabel = (
  value: number | null,
  t: (key: string, fallback?: string) => string,
) => {
  if (value === null || value === undefined) return t('articles.meta.unknown', 'Unknown');
  const meta = ITEM_SHOW_TYPE_META[value];
  return meta ? t(meta.key, meta.fallback) : t('articles.meta.unknown', 'Unknown');
};
