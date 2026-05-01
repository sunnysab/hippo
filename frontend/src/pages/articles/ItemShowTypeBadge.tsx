import { ITEM_SHOW_TYPE_META } from '../../utils/constants';
import { useI18n } from '../../i18n';
import { getItemShowTypeLabel } from './itemShowType';

interface ItemShowTypeBadgeProps {
  value: number | null;
  compact?: boolean;
  className?: string;
}

export function ItemShowTypeBadge({
  value,
  compact = false,
  className = '',
}: ItemShowTypeBadgeProps) {
  const { t } = useI18n();

  if (value === null || value === undefined) return null;
  const meta = ITEM_SHOW_TYPE_META[value];
  if (!meta) return null;

  const compactClass = compact ? ' item-show-type-badge-compact' : '';
  const extraClass = className ? ` ${className}` : '';

  return (
    <span className={`item-show-type-badge item-show-type-${meta.tone}${compactClass}${extraClass}`}>
      {getItemShowTypeLabel(value, t)}
    </span>
  );
}
